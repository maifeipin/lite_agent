"""
Review Meeting 多模型评判委员会业务服务 (features.review_meeting.decision_service)。
从 skills/ops_decision.py 迁入委员会多专家盲审、加权评分及分歧检测逻辑。
"""
import sys
import os
import json
import uuid
import time
import concurrent.futures
import re
from typing import List, Dict, Literal, Optional
from pydantic import BaseModel, Field

from lite_agent.core.config_loader import load_config
from lite_agent.core.model_router import ModelRouter
from lite_agent.core.llm_gateway import LLMGateway
from lite_agent.core.execution_ledger import ExecutionLedger
from lite_agent.core.execution import ExecutionSource


# =========================================================
# 1. Schema 定义 (稳定输出协议)
# =========================================================
class CriteriaScore(BaseModel):
    name: str = Field(description="维度名称")
    score: int = Field(ge=0, le=100, description="该维度的得分(0-100)")
    rationale: str = Field(description="打分理由")


class BaseScores(BaseModel):
    profitability: int = Field(ge=0, le=100)
    execution: int = Field(ge=0, le=100)
    timeliness: int = Field(ge=0, le=100)


class DecisionResult(BaseModel):
    decision_type: Literal["值得执行", "高风险放弃", "暂缓观察"] = Field(description="结论类型方向")
    base_scores: BaseScores = Field(description="固定维度得分")
    extra_scores: List[CriteriaScore] = Field(description="额外维度的得分", default_factory=list)
    model_reported_overall_score: int = Field(ge=0, le=100, description="你主观认为的综合得分（仅用于审计对比，最终总分由系统引擎独立加权核算）")
    confidence_score: int = Field(ge=0, le=100, description="你对本次打分的整体置信度")
    reasoning: str = Field(description="核心论证逻辑")
    evidence_gaps: List[str] = Field(description="缺失的证据或前提假设", default_factory=list)
    action_item: str = Field(description="下一步建议的行动指令")


class PeerReview(BaseModel):
    reviewed_model_id: str
    valid_points: List[str] = Field(description="对方有道理的观点")
    missing_evidence: List[str] = Field(description="对方缺失的证据")
    logic_errors: List[str] = Field(description="对方推理错误或事实遗漏的地方")
    disagreement_type: Literal["fact", "weight", "preference", "uncertainty"] = Field(description="分歧类型")
    suggested_score_delta: int = Field(ge=-20, le=20, description="建议总分调整量")
    should_escalate_to_human: bool = Field(description="是否因为分歧严重而必须交由人类裁决")


try:
    DecisionResult.model_json_schema()
    PeerReview.model_json_schema()
except Exception:
    pass


# =========================================================
# 2. 核心调度与实现
# =========================================================
def get_models_from_config() -> List[str]:
    cfg = load_config()
    return cfg.get("committee", {}).get("models", ["flash", "gemini-pro"])


def get_task_profile(task_type: str) -> dict:
    cfg = load_config()
    profiles = cfg.get("committee", {}).get("task_profiles", {})
    return profiles.get(task_type, {
        "base_weights": {
            "profitability": 0.4,
            "execution": 0.3,
            "timeliness": 0.3
        },
        "extra_criteria": [],
        "description": "通用决策评估"
    })


def build_decision_brief(gateway: LLMGateway, topic: str) -> str:
    if len(topic) < 8000:
        return topic
    prompt = f"请提炼以下长文本的核心事实基线，用于后续公平决策投票。请包含：Key Facts, Quantitative Signals, Known Uncertainties, Source Coverage。\n\n{topic[:30000]}"
    try:
        response = gateway.invoke_sync(
            [{"role": "user", "content": prompt}],
            model="flash", role="committee_brief",
            session_key="committee:brief", source=ExecutionSource.DIRECT,
            max_tokens=2048, timeout=60.0,
        )
        return response["content"]
    except Exception:
        return topic[:8000] + "\n...[Truncated and Brief Failed]"


def call_model(gateway: LLMGateway, model_name: str, prompt: str, schema_cls,
               session_key: str = "") -> str:
    """并发调用大模型，并强制输出指定 Schema"""
    sys_prompt = f"You are an expert AI committee judge. You must output valid JSON matching exactly this schema:\n{schema_cls.model_json_schema()}"

    try:
        response = gateway.invoke_sync(
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt},
            ],
            model=model_name, role="committee_judge",
            session_key=session_key, source=ExecutionSource.DIRECT,
            response_format={"type": "json_object"}, timeout=60.0,
        )
        return response["content"]
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def compute_weighted_score(res: DecisionResult, profile: dict) -> float:
    base_weights = profile.get("base_weights", {})
    extra_criteria = profile.get("extra_criteria", [])

    total_score = 0.0
    total_weight = 0.0

    for k, v in dict(res.base_scores).items():
        weight = base_weights.get(k, 0)
        total_score += v * weight
        total_weight += weight

    extra_weight_map = {c["name"]: c.get("weight", 0) for c in extra_criteria}
    for criteria in res.extra_scores:
        weight = extra_weight_map.get(criteria.name, 0)
        total_score += criteria.score * weight
        total_weight += weight

    if total_weight > 0:
        return total_score / total_weight
    return float(res.model_reported_overall_score)


def calculate_std_dev(scores: List[float]) -> float:
    if len(scores) < 2:
        return 0.0
    mean = sum(scores) / len(scores)
    variance = sum((x - mean) ** 2 for x in scores) / len(scores)
    return variance ** 0.5


def run_ops_decision(task_type: str, topic: str, session_id: str = '', trace_id: str = '') -> str:
    router = ModelRouter(load_config())
    gateway = LLMGateway(router, ExecutionLedger())
    models = get_models_from_config()
    if not models:
        return "🚨 未配置或获取到委员会模型 (committee.models 为空)"
    profile = get_task_profile(task_type)
    run_id = str(uuid.uuid4())[:8]

    brief = build_decision_brief(gateway, topic)
    prompt = f"任务配置指南与维度定义: {json.dumps(profile, ensure_ascii=False)}\n\n请严格按照 Schema 对以下简报事实进行打分和评价：\n\n{brief}"

    results = {}
    computed_scores = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as executor:
        audit_session = session_id or f"committee:{run_id}"
        future_to_model = {
            executor.submit(
                call_model, gateway, m, prompt, DecisionResult, audit_session
            ): m for m in models
        }
        for future in concurrent.futures.as_completed(future_to_model):
            m = future_to_model[future]
            res_str = future.result()
            try:
                start = res_str.find('{')
                end = res_str.rfind('}') + 1
                if start != -1 and end != 0:
                    res_str = res_str[start:end]

                res_obj = DecisionResult.model_validate_json(res_str)
                c_score = compute_weighted_score(res_obj, profile)
                results[m] = {"res": res_obj, "computed_score": c_score}
                computed_scores.append(c_score)
            except Exception as e:
                results[m] = {"error": f"解析失败 ({e}) | Raw: {res_str[:150]}"}

    audit_data = {
        "run_id": run_id,
        "task_type": task_type,
        "trace_id": trace_id or "",
        "session_id": session_id or "",
        "created_at": time.time(),
        "profile": profile,
        "brief": brief,
        "results": {k: (v["res"].model_dump() if "res" in v else v) for k, v in results.items()}
    }
    from lite_agent.core.constants import PROJECT_ROOT
    project_root = load_config().get("project_root", PROJECT_ROOT)

    def sanitize_comp(comp: str) -> str:
        return re.sub(r'[^a-zA-Z0-9_\-]', '_', comp.strip())

    safe_task_type = sanitize_comp(task_type)
    safe_trace_dir = sanitize_comp(trace_id) or "no_trace"
    safe_run_id = sanitize_comp(run_id)

    audit_dir = os.path.join(project_root, "data", "committee", safe_task_type, safe_trace_dir, safe_run_id)
    os.makedirs(audit_dir, exist_ok=True)
    audit_path = os.path.join(audit_dir, "audit.json")
    with open(audit_path, "w", encoding="utf-8") as f:
        json.dump(audit_data, f, ensure_ascii=False, indent=2)

    if not computed_scores:
        return f"🚨 所有模型评估失败。详情见审计: {audit_path}"

    std_dev = calculate_std_dev(computed_scores)

    valid_decisions = [v["res"].decision_type.strip().lower() for v in results.values() if "res" in v]
    direction_conflict = len(set(valid_decisions)) > 1

    output = [f"📊 委员会评审完毕 [RunID: {run_id}] | 得分标准差: {std_dev:.2f}", ""]

    for m, data in results.items():
        if "res" in data:
            res = data["res"]
            score = data["computed_score"]
            output.append(f"### 🤖 评委 [{m}]: `引擎核算分: {score:.1f}` vs `自报分: {res.model_reported_overall_score}` (置信度: {res.confidence_score}) | 结论: `{res.decision_type}`")
            output.append(f"- **核心理由**: {res.reasoning}")
            if res.evidence_gaps:
                output.append(f"- **证据缺失**: {', '.join(res.evidence_gaps)}")
            output.append(f"- **行动项**: {res.action_item}\n")
        else:
            output.append(f"### 🤖 评委 [{m}]: 评估失败")
            output.append(f"- {data['error']}\n")

    output.append("---")
    output.append(f"📂 机器审计日志已归档: `{audit_path}`")

    valid_count = len(valid_decisions)
    if valid_count < 2:
        output.append("\n⚠️ **警告：有效评委不足 2 名，无法形成有效共识**")
    elif std_dev > 25 or direction_conflict:
        output.append("\n🚨 **高级警报：模型间存在严重分歧**")
        if direction_conflict:
            output.append("> 原因：各模型的 `decision_type` 核心结论方向不一致。")
        else:
            output.append("> 原因：总分标准差大于 25。")
        output.append("> 已熔断自动判定，请人类介入查阅上方 reasoning。")
    elif std_dev > 12:
        output.append("\n⚠️ **警告：模型中度分歧 (标准差>12)**")
        output.append("> (V2将触发 PeerReview 进行结构化纠错)")
    else:
        output.append("\n✅ **委员会达成高度共识**")
        output.append("> 可以信任并执行评委们给出的下一步 Action Item。")

    return "\n".join(output)
