from __future__ import annotations

from .base import FetchContext, RawJob, Source
from .google_jobs import GoogleJobsSource
from .job_boards import HimalayasSource, JobicySource, RemoteOKSource, RemotiveSource, WeWorkRemotelySource
from .web_search import WebSearchSource
from .x_api import XApiSource


def all_sources() -> list[Source]:
    return [
        GoogleJobsSource(),
        WebSearchSource(),
        XApiSource(),
        RemotiveSource(),
        RemoteOKSource(),
        HimalayasSource(),
        JobicySource(),
        WeWorkRemotelySource(),
    ]


__all__ = ["FetchContext", "RawJob", "Source", "all_sources"]
