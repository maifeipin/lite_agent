"""Thin entry point for synchronous LLM calls.

ModelRouter owns routing/client construction, ModelInvoker owns provider protocol
details, and ExecutionLedger owns persistence.  This gateway only connects the
three so callers cannot accidentally skip usage accounting.
"""

from core.execution import ActorType, ExecutionContext, ExecutionSource
from core.execution_budget import estimate_input_tokens, BudgetAdmissionError
from core.model_event import ModelEvent, ModelEventType


class LLMGateway:
    def __init__(self, router=None, ledger=None, budget=None, budget_role='worker',
                 estimate_margin=1.25):
        self.router = router
        self.ledger = ledger
        self.budget = budget
        self.budget_role = budget_role
        self.estimate_margin = estimate_margin

    def invoke_sync(self, messages, tools=None, **kwargs):
        reservation = self._reserve(messages, tools, kwargs)
        result = None
        try:
            result = self._invoke_sync(messages, tools, **kwargs)
            if reservation and not result.get('usage_total'):
                result = dict(result)
                result['usage_total'] = reservation[1]
                result['usage_estimated'] = True
            return result
        finally:
            if reservation:
                self.budget.settle(reservation[0], None if (result or {}).get('usage_estimated')
                                   else (result or {}).get('usage_total'))

    def _reserve(self, messages, tools, kwargs):
        if self.budget is None:
            return None
        estimate = estimate_input_tokens(messages, tools, self.estimate_margin)
        available_output = self.budget.snapshot().remaining_tokens - estimate
        if available_output < 1:
            raise BudgetAdmissionError('输入预计用量超过剩余预算，未调用模型')
        kwargs['max_tokens'] = min(int(kwargs.get('max_tokens', 2048) or 2048), available_output)
        # Retries must be visible to this boundary, not hidden inside an SDK.
        kwargs['max_retries'] = 0
        estimate += kwargs['max_tokens']
        return self.budget.reserve(kwargs.get('role', self.budget_role), estimate), estimate

    def _normalize_usage(self, result, messages, tools, kwargs):
        if self.budget is not None and not result.get('usage_total'):
            result = dict(result)
            result['usage_total'] = estimate_input_tokens(messages, tools, self.estimate_margin) + int(kwargs.get('max_tokens', 2048))
            result['usage_estimated'] = True
        return result

    def _invoke_sync(self, messages: list, tools: list = None, *, model: str = "", invoker=None,
                    role: str = "llm", provider: str = "",
                    session_key: str = "", parent_execution_id: str = "",
                    source: ExecutionSource = ExecutionSource.DIRECT,
                    **kwargs) -> dict:
        """Invoke one model and, when configured, record one child execution.

        ``invoker`` supports existing callers that already selected a model.
        New callers should pass a configured ``model`` name and let the router
        build the provider adapter.  Ledger recording is deliberately optional
        so tests and standalone utilities remain lightweight.
        """
        if invoker is None:
            if self.router is None or not model:
                raise ValueError("model or invoker is required")
            invoker = self.router.get_invoker(model, **kwargs)
            if invoker is None:
                raise RuntimeError(f"Model is not available: {model}")

        if not provider and self.router is not None and model:
            provider = self.router.get_driver(model)
        if self.ledger is None:
            return self._normalize_usage(invoker.invoke_sync(messages=messages, tools=tools, **kwargs), messages, tools, kwargs)

        max_output_tokens = int(
            kwargs.get("max_tokens", getattr(invoker, "max_tokens", 0)) or 0
        )
        ctx = ExecutionContext(
            actor_id=role,
            actor_type=ActorType.SYSTEM,
            source=source,
            session_key=session_key,
            max_steps=1,
            max_output_tokens=max_output_tokens,
        )
        execution = self.ledger.start(
            ctx,
            model_name=getattr(invoker, "model_name", model),
            provider=provider,
            parent_execution_id=parent_execution_id,
            stream_mode=False,
        )
        try:
            self.ledger.record_and_project(
                execution.id, 0, "STEP_START", {"step": 1, "max_steps": 1}, step=1
            )
            result = invoker.invoke_sync(messages=messages, tools=tools, **kwargs)
            result = self._normalize_usage(result, messages, tools, kwargs)
            prompt_tokens = int(result.get("prompt_tokens", 0) or 0)
            completion_tokens = int(result.get("completion_tokens", 0) or 0)
            total_tokens = int(result.get("usage_total", 0) or 0)
            self.ledger.record_and_project(
                execution.id, 1, "USAGE",
                {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "estimated": bool(result.get('usage_estimated')),
                },
                step=1,
            )
            self.ledger.record_and_project(
                execution.id, 2, "DONE",
                {
                    "content": "",
                    "usage_total": total_tokens,
                    "finish_reason": result.get("finish_reason", "stop"),
                },
                step=1,
            )
            return result
        except Exception:
            self.ledger.record_and_project(
                execution.id, 1, "ERROR", {"msg": f"{role} model call failed"}, step=1
            )
            self.ledger.finish(
                execution.id, status="failed", terminal_reason="model_exception"
            )
            raise

    def invoke_stream(self, messages: list, tools: list, *, model: str = "",
                      invoker=None, **kwargs):
        """Return the provider stream through the single call boundary.

        Streaming runtime calls are recorded by ``RuntimeRecorder`` because
        usage and tool events are only known while the stream is consumed.
        Keeping provider dispatch here prevents Runtime/Worker from growing
        another routing or provider-specific branch.
        """
        if invoker is None:
            if self.router is None or not model:
                raise ValueError("model or invoker is required")
            invoker = self.router.get_invoker(model, **kwargs)
            if invoker is None:
                raise RuntimeError(f"Model is not available: {model}")
        return self._budgeted_stream(invoker, messages, tools, kwargs)

    def _budgeted_stream(self, invoker, messages, tools, kwargs):
        reservation = self._reserve(messages, tools, kwargs)
        usage = None
        try:
            for event in invoker.invoke_stream(messages, tools, **kwargs):
                if event.type == ModelEventType.USAGE:
                    usage = event.data.get('total_tokens') or None
                yield event
            if reservation and usage is None:
                yield ModelEvent(ModelEventType.USAGE, {
                    'prompt_tokens': 0, 'completion_tokens': 0,
                    'total_tokens': reservation[1], 'estimated': True,
                })
        finally:
            if reservation:
                self.budget.settle(reservation[0], usage)
