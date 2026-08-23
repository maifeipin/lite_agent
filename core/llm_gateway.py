"""Thin entry point for synchronous LLM calls.

ModelRouter owns routing/client construction, ModelInvoker owns provider protocol
details, and ExecutionLedger owns persistence.  This gateway only connects the
three so callers cannot accidentally skip usage accounting.
"""

from core.execution import ActorType, ExecutionContext, ExecutionSource


class LLMGateway:
    def __init__(self, router=None, ledger=None):
        self.router = router
        self.ledger = ledger

    def invoke_sync(self, messages: list, *, model: str = "", invoker=None,
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
            return invoker.invoke_sync(messages=messages, **kwargs)

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
            result = invoker.invoke_sync(messages=messages, **kwargs)
            prompt_tokens = int(result.get("prompt_tokens", 0) or 0)
            completion_tokens = int(result.get("completion_tokens", 0) or 0)
            total_tokens = int(result.get("usage_total", 0) or 0)
            self.ledger.record_and_project(
                execution.id, 1, "USAGE",
                {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
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
