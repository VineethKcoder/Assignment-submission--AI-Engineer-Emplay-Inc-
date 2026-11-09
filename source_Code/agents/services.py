"""Shared service container (search engine, LLM client, settings, field config)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from agents.fields import FieldConfig, load_field_config
from core.llm import LLMClient
from core.settings import Settings, get_settings
from core.tracing import Tracer
from search.engine import SearchEngine


@dataclass
class Services:
    settings: Settings
    engine: SearchEngine
    llm: LLMClient
    fields: FieldConfig

    def new_tracer(self, mode: str) -> Tracer:
        s = self.settings
        return Tracer(s.path(s.output_dir) / "traces", mode=mode,
                      price_in_per_m=s.llm_price_input_per_m, price_out_per_m=s.llm_price_output_per_m)


def build_services(settings: Settings | None = None, engine: SearchEngine | None = None,
                   llm: LLMClient | None = None) -> Services:
    settings = settings or get_settings()
    return Services(
        settings=settings,
        engine=engine or SearchEngine(settings),
        llm=llm or LLMClient(settings),
        fields=load_field_config(str(settings.path(settings.fields_config))),
    )


@lru_cache(maxsize=1)
def get_services() -> Services:
    return build_services()
