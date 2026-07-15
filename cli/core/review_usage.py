from __future__ import annotations

from dataclasses import dataclass

from codex.protocol import types as protocol


@dataclass(frozen=True)
class ReviewUsage:
    response_count: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int


class ReviewUsageAccumulator:
    def __init__(self) -> None:
        self._seen_totals: set[tuple[str, int, int, int, int, int]] = set()
        self._response_count = 0
        self._input_tokens = 0
        self._cached_input_tokens = 0
        self._output_tokens = 0
        self._reasoning_output_tokens = 0
        self._total_tokens = 0

    def record(self, event: protocol.ThreadTokenUsageUpdatedNotificationModel) -> None:
        total = event.params.tokenUsage.total
        total_key = (
            event.params.threadId,
            total.inputTokens,
            total.cachedInputTokens,
            total.outputTokens,
            total.reasoningOutputTokens,
            total.totalTokens,
        )
        if total_key in self._seen_totals:
            return
        self._seen_totals.add(total_key)

        last = event.params.tokenUsage.last
        self._response_count += 1
        self._input_tokens += last.inputTokens
        self._cached_input_tokens += last.cachedInputTokens
        self._output_tokens += last.outputTokens
        self._reasoning_output_tokens += last.reasoningOutputTokens
        self._total_tokens += last.totalTokens

    @property
    def usage(self) -> ReviewUsage | None:
        if self._response_count == 0:
            return None
        return ReviewUsage(
            response_count=self._response_count,
            input_tokens=self._input_tokens,
            cached_input_tokens=self._cached_input_tokens,
            output_tokens=self._output_tokens,
            reasoning_output_tokens=self._reasoning_output_tokens,
            total_tokens=self._total_tokens,
        )
