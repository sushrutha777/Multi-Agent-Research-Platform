from __future__ import annotations

import json
import os
import socket
from functools import lru_cache

import boto3
from dotenv import load_dotenv


load_dotenv()


@lru_cache(maxsize=1)
def _load_secret() -> dict:
    """Load production secrets from Secrets Manager, or local settings from .env.

    The old implementation contacted AWS while the module was imported, which made
    local development and unit tests impossible. Production can opt in by setting
    AWS_SECRETS_MANAGER_SECRET_ID (the Terraform deployment does this).
    """
    secret_id = os.getenv("AWS_SECRETS_MANAGER_SECRET_ID", "")
    if secret_id:
        region = os.environ.get("AWS_REGION", "us-east-1")
        client = boto3.client("secretsmanager", region_name=region)
        response = client.get_secret_value(SecretId=secret_id)
        return json.loads(response["SecretString"])
    return dict(os.environ)


class Config:
    def __init__(self):
        data = _load_secret()

        # AWS
        self.aws_region: str = data.get("AWS_REGION", "us-east-1")
        self.environment: str = data.get("ENVIRONMENT", "local")

        # Bedrock Guardrails
        self.guardrails_enabled: bool = data.get("GUARDRAILS_ENABLED", "false").lower() == "true"
        self.bedrock_guardrail_id: str = data.get("BEDROCK_GUARDRAIL_ID", "")
        self.bedrock_guardrail_version: str = data.get("BEDROCK_GUARDRAIL_VERSION", "")

        # Storage
        self.redis_url: str = data.get("REDIS_URL", "redis://localhost:6379/0")
        self.database_url: str = data.get(
            "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/researchdb"
        )
        self.tensorzero_url: str = data.get("TENSORZERO_URL", "http://localhost:3000")

        # Auth
        self.api_key: str = data.get("API_KEY", "")
        self.allowed_origins: list[str] = [
            origin.strip()
            for origin in data.get("ALLOWED_ORIGINS", "http://localhost:8000").split(",")
            if origin.strip()
        ]

        # Live research tools
        self.tavily_api_key: str = data.get("TAVILY_API_KEY", "")
        self.firecrawl_api_key: str = data.get("FIRECRAWL_API_KEY", "")
        self.wikipedia_enabled: bool = data.get("WIKIPEDIA_ENABLED", "true").lower() == "true"
        self.search_max_results: int = int(data.get("SEARCH_MAX_RESULTS", 5))
        self.max_tool_calls: int = int(data.get("MAX_TOOL_CALLS", 8))
        self.tool_rate_limit_per_minute: int = int(data.get("TOOL_RATE_LIMIT_PER_MINUTE", 30))
        self.tool_timeout: float = float(data.get("TOOL_TIMEOUT", 30))
        self.firecrawl_base_url: str = data.get("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev")
        self.firecrawl_allowed_domains: list[str] = [
            domain.strip().lower()
            for domain in data.get("FIRECRAWL_ALLOWED_DOMAINS", "").split(",")
            if domain.strip()
        ]
        self.firecrawl_allowed_paths: list[str] = [
            path.strip()
            for path in data.get("FIRECRAWL_ALLOWED_PATHS", "").split(",")
            if path.strip()
        ]
        self.firecrawl_max_pages: int = int(data.get("FIRECRAWL_MAX_PAGES", 3))
        self.firecrawl_max_depth: int = int(data.get("FIRECRAWL_MAX_DEPTH", 0))

        # Report cache. It is exact-key only; it is never used as vector/RAG retrieval.
        self.cache_reports: bool = data.get("CACHE_REPORTS", "false").lower() == "true"

        # LangSmith tracing
        self.langsmith_api_key: str = data.get("LANGSMITH_API_KEY", "")
        self.langchain_project: str = data.get("LANGCHAIN_PROJECT", "research-agent")
        self.langsmith_dataset: str = data.get("LANGSMITH_DATASET", "research-agent-reports")

        # Semantic cache
        self.cache_ttl: int = int(data.get("CACHE_TTL", 3600))
        self.cache_similarity_threshold: float = float(data.get("CACHE_SIMILARITY_THRESHOLD", 0.85))

        # Session memory
        self.session_ttl: int = int(data.get("SESSION_TTL", 1800))
        self.session_max_messages: int = int(data.get("SESSION_MAX_MESSAGES", 5))
        self.session_content_truncate: int = int(data.get("SESSION_CONTENT_TRUNCATE", 500))

        # Long-term memory
        self.ltm_diff_limit: int = int(data.get("LTM_DIFF_LIMIT", 5))
        self.report_history_days: int = int(data.get("REPORT_HISTORY_DAYS", 30))

        # Job queue
        self.stream_key: str = data.get("STREAM_KEY", "research:jobs")
        self.consumer_group: str = data.get("CONSUMER_GROUP", "workers")
        # hostname = unique per ECS task = safe for horizontal scaling
        self.consumer_name: str = data.get("CONSUMER_NAME", socket.gethostname())
        self.result_ttl: int = int(data.get("RESULT_TTL", 3600))
        self.dead_letter_stream: str = data.get("DEAD_LETTER_STREAM", "research:dead-letter")
        self.job_max_retries: int = int(data.get("JOB_MAX_RETRIES", 2))
        self.job_claim_idle_ms: int = int(data.get("JOB_CLAIM_IDLE_MS", 120000))
        self.job_timeout: int = int(data.get("JOB_TIMEOUT", 600))
        self.worker_concurrency: int = int(data.get("WORKER_CONCURRENCY", 2))
        self.start_worker: bool = data.get("START_WORKER", "true").lower() == "true"

        # Agent tuning
        self.agent_report_truncate: int = int(data.get("AGENT_REPORT_TRUNCATE", 3000))
        self.agent_max_iterations: int = int(data.get("AGENT_MAX_ITERATIONS", 2))

        # Eval tuning
        self.eval_report_truncate: int = int(data.get("EVAL_REPORT_TRUNCATE", 1500))
        self.eval_comment_truncate: int = int(data.get("EVAL_COMMENT_TRUNCATE", 300))

        # LLM retry
        self.llm_max_retries: int = int(data.get("LLM_MAX_RETRIES", 3))
        self.llm_retry_delay: float = float(data.get("LLM_RETRY_DELAY", 1.0))

        # Rate limiting (per IP, per window)
        self.rate_limit_requests: int = int(data.get("RATE_LIMIT_REQUESTS", 10))
        self.rate_limit_window: int = int(data.get("RATE_LIMIT_WINDOW", 60))

        # DB connection pool
        self.db_pool_min: int = int(data.get("DB_POOL_MIN", 2))
        self.db_pool_max: int = int(data.get("DB_POOL_MAX", 10))

        if self.langsmith_api_key:
            os.environ["LANGCHAIN_TRACING_V2"] = "true"
            os.environ["LANGCHAIN_API_KEY"] = self.langsmith_api_key
            os.environ["LANGCHAIN_PROJECT"] = self.langchain_project
            os.environ["LANGCHAIN_ENDPOINT"] = "https://api.smith.langchain.com"
