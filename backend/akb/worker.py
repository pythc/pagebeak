import logging
import time
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import InterfaceError, OperationalError

from akb.config import Settings
from akb.db import Database
from akb.models import Claim, Evidence, Job, Source, now, uid
from akb.object_store import local_original
from akb.organization import enqueue, reconcile
from akb.parse_runner import parse_isolated
from akb.parsing import ParseError, category_for, extract_labeled
from akb.services import clean_source, lock_tenant

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.worker_database_url)
        self.id = str(uuid4())
        self.last_reap = 0
        self.last_native_sweep = 0

    def claim(self):
        with self.db.session() as session:
            row = (
                session.execute(
                    text("SELECT * FROM public.akb_claim_job(:worker, :lease)"),
                    {"worker": self.id, "lease": self.settings.lease_seconds},
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def _current_job(self, session, ticket):
        job = session.scalar(
            select(Job)
            .where(Job.id == ticket["job_id"])
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            not job
            or job.status != "running"
            or job.worker_id != self.id
            or job.fencing_token != ticket["fencing_token"]
            or job.lease_until <= now()
        ):
            return None
        return job

    def process(self, ticket):
        tenant_id = ticket["tenant_id"]
        # Organization jobs are tenant-wide and intentionally have no source_id.
        with self.db.session(tenant_id) as session:
            job = session.get(Job, ticket["job_id"])
            if job and job.kind == "native_sync":
                native_sync = True
            else:
                native_sync = False
            if job and job.kind == "organize":
                lock_tenant(session, tenant_id)
                job = self._current_job(session, ticket)
                if job:
                    if self.settings.knowledge_engine in {"tencent", "weknora"}:
                        from akb.memory_library import sync_sources

                        job.result = sync_sources(session, self.settings, tenant_id)
                        if self.settings.knowledge_engine == "weknora":
                            from akb.agent_jobs import refresh

                            refresh(session, tenant_id)
                    else:
                        reconcile(session, tenant_id, job)
                    job.status, job.lease_until = "completed", None
                    if self.settings.knowledge_engine in {"tencent", "weknora"} and job.result.get("more"):
                        job.status, job.attempts = "queued", 0
                return
        if native_sync:
            from akb.native import sync

            sync(self, ticket)
            return
        with self.db.session(tenant_id) as session:
            job = session.get(Job, ticket["job_id"])
            source = session.get(Source, job.source_id) if job else None
            if not source or job.status != "running" or job.fencing_token != ticket["fencing_token"]:
                return
            source_id, revision, kind = source.id, job.source_revision, job.kind
            filename, key = source.filename, source.object_key
            expected_sha, expected_size = source.sha256, source.size
        parsed, failure = None, None
        last_heartbeat = 0

        def heartbeat(**_progress):
            nonlocal last_heartbeat
            if time.monotonic() - last_heartbeat < 10:
                return
            with self.db.session(tenant_id) as session:
                lock_tenant(session, tenant_id)
                current = self._current_job(session, ticket)
                if not current:
                    raise OSError("任务已过期")
                current.lease_until = now() + timedelta(seconds=self.settings.lease_seconds)
            last_heartbeat = time.monotonic()

        if kind == "ingest":
            try:
                with local_original(
                    self.settings, key, expected_sha, expected_size, heartbeat=heartbeat
                ) as path:
                    parsed = parse_isolated(filename, path)
            except ParseError as exc:
                failure = (exc.code, exc.message)
            except OSError:
                if key.startswith(("cloudreve:", "native:")):
                    raise  # Provider failures use durable retry, never publish unverified parse results.
                failure = ("file_unavailable", "原件暂不可用，请检查存储")
        with self.db.session(tenant_id) as session:
            tenant = lock_tenant(session, tenant_id)
            source = session.get(Source, source_id)
            job = self._current_job(session, ticket)
            if not job:
                return
            if source.revision != revision or (kind == "ingest" and source.revoked_at):
                job.status = "cancelled"
                return
            if kind == "cleanup":
                clean_source(session, self.settings, source)
            elif failure:
                source.status = (
                    "unsupported"
                    if failure[0] in {"unsupported", "ocr_unavailable"}
                    else "password_required"
                    if failure[0] == "password_required"
                    else "failed"
                )
                source.error, source.coverage = failure[1], "unknown"
                job.status, job.error = "failed", failure[0]
                tenant.revision += 1
                enqueue(session, tenant_id)
                return
            else:
                generation_id = uid()
                for ordinal, block in enumerate(parsed.blocks):
                    evidence = Evidence(
                        tenant_id=tenant_id,
                        source_id=source_id,
                        generation_id=generation_id,
                        ordinal=ordinal,
                        locator=block.locator,
                        text=block.text,
                    )
                    session.add(evidence)
                    session.flush()
                    if source.subject_scope == "self" and self.settings.knowledge_engine not in {"tencent", "weknora"}:
                        for field, value, quote in extract_labeled(block.text):
                            session.add(
                                Claim(
                                    tenant_id=tenant_id,
                                    source_id=source_id,
                                    evidence_id=evidence.id,
                                    field=field,
                                    value=value,
                                    quote=quote,
                                    status="accepted"
                                    if field in {"name", "email"} and block.locator.get("method") != "ocr"
                                    else "candidate",
                                )
                            )
                source.generation_id, source.coverage, source.error = (
                    generation_id,
                    parsed.coverage,
                    parsed.warning,
                )
                source.status = "ready" if parsed.coverage == "complete" else "partial"
                source.category = category_for(filename, "\n".join(b.text for b in parsed.blocks[:4]))
                from akb.models import FileChange

                for event in session.scalars(select(FileChange).where(FileChange.source_id == source.id)):
                    if event.after.get("sha256") == source.sha256 and not event.after.get("generation_id"):
                        event.after = {**event.after, "generation_id": generation_id}
            job.status = "completed"
            job.lease_until = None
            tenant.revision += 1
            enqueue(session, tenant_id)

    def run_once(self):
        if self.settings.native_sync_enabled and time.monotonic() - self.last_native_sweep > 15:
            from akb import native

            with self.db.session() as session:
                tenants = list(session.scalars(text("SELECT tenant_id FROM public.akb_native_tenants()")))
            for tenant_id in tenants:
                with self.db.session(tenant_id) as session:
                    native.enqueue(session, tenant_id)
            self.last_native_sweep = time.monotonic()
        if time.monotonic() - self.last_reap > 60:
            from akb.space import expire_uploads

            with self.db.session() as session:
                tenants = list(session.scalars(text("SELECT tenant_id FROM public.akb_staging_tenants()")))
            for tenant_id in tenants:
                try:
                    with self.db.session(tenant_id) as session:
                        expire_uploads(session, self.settings, lock_tenant(session, tenant_id))
                except OSError:
                    log.error("Staging cleanup failed; retrying on the next sweep")
            if self.settings.knowledge_engine in {"tencent", "weknora"}:
                from akb.memory_library import maintain

                with self.db.session() as session:
                    memory_tenants = list(session.scalars(text("SELECT tenant_id FROM public.akb_memory_maintenance_tenants()")))
                from akb.memory_runtime import pending_tenants

                memory_tenants = sorted(set(memory_tenants) | set(pending_tenants(self.settings)))
                for tenant_id in memory_tenants:
                    try:
                        with self.db.session(tenant_id) as session:
                            maintain(session, self.settings, tenant_id)
                    except Exception:
                        log.warning("Memory cleanup pending; retrying on the next sweep")
            self.last_reap = time.monotonic()
        ticket = self.claim()
        if not ticket:
            return False
        try:
            self.process(ticket)
        except Exception:
            # Do not log document text, paths, SQL parameters, or credentials.
            log.error("Worker failed for job %s; lease permits retry", ticket["job_id"])
            with self.db.session(ticket["tenant_id"]) as session:
                tenant = lock_tenant(session, ticket["tenant_id"])
                job = self._current_job(session, ticket)
                if job:
                    job.status = "failed" if job.attempts >= 5 else "queued"
                    job.error = "processing_error"
                    job.lease_until = now() + timedelta(seconds=min(2**job.attempts, 60))
                    if job.kind == "native_sync":
                        from akb.models import NativeConnection

                        connection = session.scalar(select(NativeConnection))
                        if connection:
                            connection.error = "同步未完成，请检查文件空间连接；系统会自动重试"
                    if job.status == "failed" and job.kind == "ingest":
                        source = session.get(Source, job.source_id)
                        if not source.revoked_at:
                            source.status, source.error = "failed", "任务重试次数耗尽，请检查后台服务"
                            tenant.revision += 1
                            enqueue(session, ticket["tenant_id"])
        return True

    def close(self):
        self.db.close()


def run_worker(worker):
    delay = 1
    while True:
        try:
            worked = worker.run_once()
        except (OperationalError, InterfaceError):
            # Session contexts roll back. Claimed jobs keep their fencing token
            # and are reclaimed by lease expiry; do not mark an outage as failure.
            log.warning("Database unavailable; worker will retry in %s seconds", delay)
            time.sleep(delay)
            delay = min(delay * 2, 10)
            continue
        delay = 1
        if not worked:
            time.sleep(0.5)


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    worker = Worker(Settings())
    try:
        run_worker(worker)
    except KeyboardInterrupt:
        pass
    finally:
        worker.close()
