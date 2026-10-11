"""A bounded billing workflow: fetch, snapshot, write, deliver. No tool loop."""
import json
import os
import re
import subprocess
from core.billing_snapshot import build_snapshot, model_evidence, render_report
from core.execution import ExecutionContext, ActorType, ExecutionSource
from core.execution_budget import ExecutionBudget
from core.llm_gateway import LLMGateway
from core.model_policy import ExecutionPolicy, ModelSelector
from core.output_delivery import _deliver_complete, resolve_output_policy, archive_output
from core.task_result import TaskResult
from core.utils.masker import mask_secrets

from integrations.mail.sync_receipt import (
    parse_json_summary,
    summarize_account_errors,
    _JSON_SUMMARY_RE
)
from integrations.mail.sync_service import sync_and_consume

# 保持兼容导出
_parse_json_summary = parse_json_summary


class BillingWorkflow:
    def __init__(self, config, router, ledger=None):
        self.config, self.router, self.ledger = config, router, ledger
        self.settings = (config.get('task_routing') or {}).get('billing_workflow') or {}

    def matches(self, goal):
        text = goal.lower()
        # Narrow opt-in: configured destination and latest billing retrieval /
        # summary only. Explicit recipients or extra operations stay with DAG.
        return bool(self.settings.get('enabled', False) and '账单' in text and
                    any(word in text for word in ('拉取', '最新')) and
                    any(word in text for word in ('分析', '总结')) and
                    not re.search(r'[\w.+-]+@[\w.-]+\.[a-z]{2,}', text) and
                    not any(word in text for word in ('删除', '转账', '标记', '还款操作', '投资建议')))

    def fetch(self, script_dir, months):
        # 委托统一同步服务：单次 sync3m 流水线并自动有上限消费落库的质量事件。
        stmt_db = (self.config.get('billing') or {}).get('statement_db_path') or os.path.join(script_dir, 'statements.db')
        todo_db = (self.config.get('billing') or {}).get('todo_db_path')
        result = sync_and_consume(
            script_dir=script_dir,
            statement_db_path=stmt_db,
            todo_db_path=todo_db,
            months=months,
            runner=subprocess.run
        )
        all_errors = list(dict.fromkeys(result['fetch_errors'] + result['quality_errors']))
        return result['fetch_log'], all_errors

    @staticmethod
    def _summary_errors(summary):
        return summarize_account_errors(summary)

    def run(self, goal, session_key, policy=None):
        policy = policy or ExecutionPolicy()
        ceiling = int(self.settings.get('max_total_tokens', 60000))
        if policy.max_total_tokens is not None:
            ceiling = min(ceiling, policy.max_total_tokens)
        calls = max(1, min(2, int(self.settings.get('analysis_max_calls', 2))))
        if policy.max_steps is not None:
            calls = max(1, min(calls, policy.max_steps))
        budget = ExecutionBudget(calls, ceiling)
        routing = self.config.get('task_routing') or {}
        control = routing.get('budget_control') or {}
        script_dir = (self.config.get('billing') or {}).get('script_dir', '/home/liteagent/mail-statement-parser')
        db_path = (self.config.get('billing') or {}).get('statement_db_path', os.path.join(script_dir, 'statements.db'))
        months = 1
        match = re.search(r'(?:最近|近)\s*(\d+)\s*个?月', goal)
        if match:
            months = max(1, min(24, int(match.group(1))))
        parent = None
        result = TaskResult('failed', '', failed_nodes=[])
        try:
            if self.ledger:
                parent = self.ledger.start(ExecutionContext(
                    actor_id='billing_workflow', actor_type=ActorType.SYSTEM,
                    source=ExecutionSource.ORCHESTRATOR, session_key=session_key,
                    max_steps=calls, token_budget=ceiling,
                ), model_name='', provider='billing_workflow', stream_mode=False)
            print('[BILLING] deterministic fetch started')
            fetch_log, fetch_errors = self.fetch(script_dir, months)
            snapshot = build_snapshot(db_path, months, self.settings.get('detail_limit', 20))
            snapshot['anomalies'] += fetch_errors
            result.failed_nodes = ['账单同步或质量待办'] if fetch_errors else []
            print(f"[BILLING] snapshot statements={snapshot['statement_count']} transactions={snapshot['transaction_count']} anomalies={len(snapshot['anomalies'])}")
            messages = [
                {'role': 'system', 'content': '根据提供的账单快照撰写中文分析。不得调用工具。区分数据库标记与实际还款；不得跨币种合计。交易样例不是全量，不能从样例推断全量消费分布。缺失数据不能算零。遵守每张账单的 purpose_availability：blocked 用途不得给出确定结论，partial 或 transaction_totals_complete=false 必须标明不完整。原币币种或币种来源未确认时，不猜测币种、不计算汇率。不得依据地区、商户或金额比例推断币种。不提供投资建议。明确列出异常、数据时效和需核实事项。'},
                {'role': 'user', 'content': json.dumps(model_evidence(snapshot), ensure_ascii=False)},
            ]
            analysis = ''
            analysis_failed = False
            try:
                decision = ModelSelector(self.config).select('worker', subtask_type='data_analysis', policy=policy,
                    preferred_model=self.settings.get('analysis_model', ''))
                profile = self.router.get_call_profile(decision.model, 'chat')
                max_output = min(int(self.settings.get('analysis_max_output_tokens', 2048)), profile['max_tokens'])
                invoker = self.router.get_invoker(decision.model, profile='chat', max_tokens=max_output)
                if not invoker:
                    raise RuntimeError('已选择的分析模型不可用')
                gateway = LLMGateway(self.router, self.ledger, budget,
                                     estimate_margin=float(control.get('request_estimate_margin', 1.25)))
                for attempt in range(calls):
                    response = gateway.invoke_sync(messages, [], invoker=invoker, model=decision.model,
                        provider=self.router.get_driver(decision.model), role='billing_analysis',
                        session_key=session_key, parent_execution_id=parent.id if parent else '',
                        max_tokens=max_output, timeout=profile['timeout'], **profile['invoke_kwargs'])
                    piece = str(response.get('content') or '').strip()
                    analysis += piece
                    if response.get('finish_reason') != 'length':
                        break
                    if not piece:
                        analysis_failed = True
                        snapshot['anomalies'].append('模型耗尽输出额度但没有生成正文，已停止重复请求；需核对模型思考参数。')
                        break
                    if attempt + 1 == calls:
                        analysis_failed = True
                        snapshot['anomalies'].append('模型分析达到输出上限；下方确定性完整明细不受影响。')
                        break
                    messages += [{'role': 'assistant', 'content': piece},
                                 {'role': 'user', 'content': '只补充被截断的分析，不重复前文。'}]
                if not analysis:
                    raise RuntimeError('模型返回空分析')
            except Exception as exc:
                analysis_failed = True
                snapshot['anomalies'].append(f'模型分析未完成：{type(exc).__name__}；保留确定性明细与异常检查。')
            if analysis_failed:
                result.failed_nodes.append('模型分析')
            result.usage = {'total_tokens': budget.snapshot().used_tokens,
                            'estimated_tokens': budget.snapshot().estimated_tokens,
                            'max_tokens': ceiling, 'calls': budget.snapshot().used_steps,
                            'by_role': budget.usage_by_role()}
            result.status = 'partial' if result.failed_nodes or not snapshot['statement_count'] else 'succeeded'
            if not snapshot['statement_count']:
                result.failed_nodes.append('账单数据为空')
            result.content = render_report(snapshot, analysis, fetch_log, result.usage)
            title = f"账单明细与异常 {snapshot['as_of']}"
            archive_id = archive_output(result.status_text() + '\n\n' + result.content, 'billing', self.config, '账单明细与异常')
            delivery_policy = resolve_output_policy(self.config)
            destination, location = _deliver_complete(result.status_text() + '\n\n' + result.content,
                'billing', self.config, delivery_policy, title)
            # Public fallback is permitted only when this request explicitly
            # includes HedgeDoc as an alternative destination.
            if destination in ('', 'sqlite') and re.search(r'he(?:rdge|dge)doc', goal, re.I):
                destination, location = _deliver_complete(result.status_text() + '\n\n' + result.content,
                    'billing', self.config, resolve_output_policy(self.config, {'full_delivery': 'hedgedoc'}), title)
            submitted = destination in ('email', 'hedgedoc')
            result.delivery = {'purpose': 'billing_report', 'status': 'submitted' if submitted else 'archived',
                               'destination': destination, 'location': location, 'archive_id': archive_id}
            if not submitted:
                result.status = 'partial'
                result.failed_nodes.append('外部投递')
            print(f'[BILLING] status={result.status} tokens={result.usage["total_tokens"]} delivery={destination}')
            return result
        finally:
            if self.ledger and parent:
                self.ledger.finish(parent.id, status='succeeded' if result.status == 'succeeded' else 'failed',
                                   terminal_reason='billing_' + result.status)
