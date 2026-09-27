"""The swappable source interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from rplat.types import Dataset


class DataSource(ABC):
    """A vendor of point-in-time records.

    Sources differ enormously in what they carry: a free price feed has bars and
    nothing else, while a fundamentals vendor has filings with real restatement
    history. So the interface is one method keyed by dataset, with an empty
    default — a source implements only what it has, and the store asks for
    everything without knowing the difference.

    The contract a source must honour is narrow but absolute: **every record's
    ``knowledge_date`` must be the date that fact genuinely became knowable.**
    A source that stamps ``knowledge_date`` with today's date, or with the
    ``effective_date`` of a fact that was actually published later, silently
    injects look-ahead into every downstream backtest. That is the single
    assumption the rest of the platform cannot verify for you, which is why
    :mod:`rplat.sources.fixture` ships restatements and late arrivals — so the
    detectors planned for Phase 3 will have something real to catch. They are
    not built yet; the append-time check in :mod:`rplat.store.validate` is the
    only leakage detection today.
    """

    #: Short identifier recorded on every row this source writes.
    name: str = "unnamed"

    def records(self, dataset: Dataset) -> Iterable[object]:
        """Yield records for ``dataset``. Empty when unsupported."""
        return ()

    @abstractmethod
    def describe(self) -> str:
        """One line about what this source provides, for the CLI."""
