from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    display_name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(String(36), unique=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)


class Credential(Base):
    __tablename__ = "credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(80), default="browser")
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    csrf: Mapped[str] = mapped_column(String(64), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    change_cursor: Mapped[int] = mapped_column(BigInteger, default=0)
    observed_hash: Mapped[str] = mapped_column(String(64), default="")


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    quota_bytes: Mapped[int] = mapped_column(BigInteger, default=10 * 1024**3)
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    memory_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")


class AssistantClient(Base):
    __tablename__ = "assistant_clients"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    metadata_json: Mapped[dict] = mapped_column(JSON)


class SpaceDevice(Base):
    """Browser session; after identity cutover it no longer creates an owner."""
    __tablename__ = "space_devices"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    csrf: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class SpaceLink(Base):
    __tablename__ = "space_links"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    credential_id: Mapped[str] = mapped_column(ForeignKey("credentials.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExternalIdentity(Base):
    """Verified provider subject mapped to an existing, stable Pagebeak user."""
    __tablename__ = "external_identities"
    __table_args__ = (UniqueConstraint("issuer", "namespace", "subject"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    issuer: Mapped[str] = mapped_column(String(512))
    namespace: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(255))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    provisions_space: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class IdentityLogin(Base):
    """Short-lived login transaction. Upstream access/refresh tokens are never retained."""
    __tablename__ = "identity_logins"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("space_devices.id"))
    provider: Mapped[str] = mapped_column(String(80))
    configuration: Mapped[str] = mapped_column(String(64))
    nonce_digest: Mapped[str] = mapped_column(String(64))
    verifier: Mapped[str] = mapped_column(String(100))
    intent: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    reference: Mapped[str | None] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssistantGrant(Base):
    """OAuth metadata, not a second personal knowledge store. Secrets are hashed."""
    __tablename__ = "assistant_grants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(16))
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("assistant_clients.id"))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    credential_id: Mapped[str | None] = mapped_column(ForeignKey("credentials.id"))
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Owned:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)


class MemoryBinding(Owned, Base):
    __tablename__ = "memory_bindings"
    __table_args__ = (UniqueConstraint("tenant_id"),)
    engine: Mapped[str] = mapped_column(String(20), default="tencent", server_default="tencent")
    wiki_id: Mapped[str] = mapped_column(String(100))
    exports: Mapped[dict] = mapped_column(JSON, default=dict)


class LocalCatalog(Owned, Base):
    """Opt-in device inventory metadata; never local text or absolute paths."""
    __tablename__ = "local_catalogs"
    __table_args__ = (UniqueConstraint("tenant_id", "device_id"),)
    device_id: Mapped[str] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    snapshot_id: Mapped[str] = mapped_column(String(36))
    items: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LocalSourceLink(Owned, Base):
    """Version observations of already-authorized uploaded originals, not personal facts."""
    __tablename__ = "local_source_links"
    __table_args__ = (UniqueConstraint("tenant_id", "device_id", "document_id", "source_id"),)
    device_id: Mapped[str] = mapped_column(String(36))
    document_id: Mapped[str] = mapped_column(String(36))
    source_id: Mapped[str] = mapped_column(String(36))
    sha256: Mapped[str] = mapped_column(String(64))
    current_hash: Mapped[str | None] = mapped_column(String(64))
    stale: Mapped[bool] = mapped_column(Boolean, default=False)


class MemoryObject(Owned, Base):
    """Lifecycle metadata only. Page and memory bodies live in Tencent, not PostgreSQL."""
    __tablename__ = "memory_objects"
    __table_args__ = (UniqueConstraint("tenant_id", "kind", "key"),)
    kind: Mapped[str] = mapped_column(String(20))
    key: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    remote_ref: Mapped[str] = mapped_column(String(250))
    remote_ids: Mapped[list] = mapped_column(JSON, default=list)
    source_versions: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    citations: Mapped[list] = mapped_column(JSON, default=list)
    raw_refs: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    protected: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MemoryGarbage(Owned, Base):
    __tablename__ = "memory_garbage"
    kind: Mapped[str] = mapped_column(String(20))
    remote_ref: Mapped[str] = mapped_column(String(250))
    remote_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FillingReceipt(Owned, Base):
    """Only output hashes and source versions, never a duplicate fact store."""
    __tablename__ = "filling_receipts"
    __table_args__ = (UniqueConstraint("tenant_id", "request_id"),)
    request_id: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))
    value_hashes: Mapped[dict] = mapped_column(JSON, default=dict)
    source_versions: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Onboarding(Owned, Base):
    __tablename__ = "onboarding"
    __table_args__ = (UniqueConstraint("tenant_id"),)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    skipped: Mapped[list] = mapped_column(JSON, default=list)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    installation: Mapped[dict] = mapped_column(JSON, default=dict)
    credential_id: Mapped[str | None] = mapped_column(ForeignKey("credentials.id"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConnectionTicket(Owned, Base):
    __tablename__ = "connection_tickets"
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    issued_by: Mapped[str] = mapped_column(ForeignKey("credentials.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Source(Owned, Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "folder_id"], ["file_folders.tenant_id", "file_folders.id"]),
    )
    folder_id: Mapped[str | None] = mapped_column(String(36))
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    title: Mapped[str] = mapped_column(String(255))
    filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(120))
    sha256: Mapped[str] = mapped_column(String(64))
    indexed_hash: Mapped[str] = mapped_column(String(64), default="")
    origin_path: Mapped[str | None] = mapped_column(String(2048))
    object_key: Mapped[str] = mapped_column(String(200))
    size: Mapped[int] = mapped_column(BigInteger)
    subject_scope: Mapped[str] = mapped_column(String(16), default="reference")
    status: Mapped[str] = mapped_column(String(24), default="queued")
    coverage: Mapped[str] = mapped_column(String(24), default="unknown")
    error: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40), default="待整理")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    generation_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Evidence(Owned, Base):
    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "source_id"], ["sources.tenant_id", "sources.id"]),
    )
    source_id: Mapped[str] = mapped_column(String(36), index=True)
    generation_id: Mapped[str] = mapped_column(String(36))
    ordinal: Mapped[int] = mapped_column(Integer)
    locator: Mapped[dict] = mapped_column(JSON)
    text: Mapped[str] = mapped_column(Text)


class Claim(Owned, Base):
    __tablename__ = "claims"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "source_id"], ["sources.tenant_id", "sources.id"]),
        ForeignKeyConstraint(["tenant_id", "evidence_id"], ["evidence.tenant_id", "evidence.id"]),
    )
    source_id: Mapped[str | None] = mapped_column(String(36), index=True)
    evidence_id: Mapped[str | None] = mapped_column(String(36))
    field: Mapped[str] = mapped_column(String(40))
    value: Mapped[str] = mapped_column(Text)
    quote: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="candidate")
    basis: Mapped[str] = mapped_column(String(24), default="source_supported")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Decision(Owned, Base):
    __tablename__ = "decisions"
    claim_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(24))
    previous_value: Mapped[str] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Owned, Base):
    __tablename__ = "jobs"
    __table_args__ = (ForeignKeyConstraint(["tenant_id", "source_id"], ["sources.tenant_id", "sources.id"]),)
    source_id: Mapped[str | None] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="ingest")
    source_revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    fencing_token: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(36))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class Catalog(Owned, Base):
    __tablename__ = "catalogs"
    __table_args__ = (UniqueConstraint("tenant_id"),)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    manual: Mapped[dict] = mapped_column(JSON, default=dict)
    last_result: Mapped[dict] = mapped_column(JSON, default=dict)
    organized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CatalogEntry(Owned, Base):
    __tablename__ = "catalog_entries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_id"),
        ForeignKeyConstraint(["tenant_id", "source_id"], ["sources.tenant_id", "sources.id"]),
    )
    source_id: Mapped[str] = mapped_column(String(36), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    generated: Mapped[dict] = mapped_column(JSON, default=dict)
    manual: Mapped[dict] = mapped_column(JSON, default=dict)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Page(Owned, Base):
    __tablename__ = "pages"
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    evidence_ids: Mapped[list] = mapped_column(JSON)
    origin_kind: Mapped[str] = mapped_column(String(20), default="generated")
    snapshot_revision: Mapped[int | None] = mapped_column(Integer)
    fact_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FormDraft(Owned, Base):
    __tablename__ = "form_drafts"
    title: Mapped[str] = mapped_column(String(200))
    fields: Mapped[list] = mapped_column(JSON)
    answers: Mapped[list] = mapped_column(JSON)
    snapshot_revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KnowledgeTask(Owned, Base):
    __tablename__ = "knowledge_tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_id", "generation_id"),
        ForeignKeyConstraint(["tenant_id", "source_id"], ["sources.tenant_id", "sources.id"]),
    )
    source_id: Mapped[str] = mapped_column(String(36), index=True)
    generation_id: Mapped[str] = mapped_column(String(36))
    source_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="waiting_agent")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    owner_id: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KnowledgeFact(Owned, Base):
    __tablename__ = "knowledge_facts"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "source_id"], ["sources.tenant_id", "sources.id"]),
        ForeignKeyConstraint(["tenant_id", "evidence_id"], ["evidence.tenant_id", "evidence.id"]),
        UniqueConstraint("tenant_id", "dedup_key"),
    )
    source_id: Mapped[str | None] = mapped_column(String(36), index=True)
    evidence_id: Mapped[str | None] = mapped_column(String(36))
    generation_id: Mapped[str | None] = mapped_column(String(36))
    dedup_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    subject: Mapped[str] = mapped_column(String(200))
    subject_scope: Mapped[str] = mapped_column(String(16))
    entity_key: Mapped[str] = mapped_column(String(250))
    field: Mapped[str] = mapped_column(String(40))
    value: Mapped[str] = mapped_column(Text)
    polarity: Mapped[str] = mapped_column(String(16), default="positive")
    valid_from: Mapped[str | None] = mapped_column(String(10))
    valid_to: Mapped[str | None] = mapped_column(String(10))
    quote: Mapped[str] = mapped_column(Text, default="")
    support: Mapped[dict] = mapped_column(JSON, default=dict)
    basis: Mapped[str] = mapped_column(String(24), default="agent_proposed")
    status: Mapped[str] = mapped_column(String(24), default="candidate")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KnowledgeDecision(Owned, Base):
    __tablename__ = "knowledge_decisions"
    fact_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(24))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Idempotency(Owned, Base):
    __tablename__ = "idempotency"
    __table_args__ = (UniqueConstraint("tenant_id", "operation", "key"),)
    operation: Mapped[str] = mapped_column(String(100))
    key: Mapped[str] = mapped_column(String(120))
    request_hash: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(36))


class Audit(Owned, Base):
    __tablename__ = "audit"
    action: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Upload(Owned, Base):
    __tablename__ = "uploads"
    folder_id: Mapped[str | None] = mapped_column(String(36))
    filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(120))
    subject_scope: Mapped[str] = mapped_column(String(16))
    size: Mapped[int] = mapped_column(BigInteger)
    offset: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(20), default="staging")
    source_id: Mapped[str | None] = mapped_column(String(36))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CleanupPlan(Owned, Base):
    __tablename__ = "cleanup_plans"
    mode: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    manifest: Mapped[list] = mapped_column(JSON)
    summary: Mapped[str] = mapped_column(Text, default="")
    authorizer_id: Mapped[str | None] = mapped_column(String(36))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class StorageArchive(Owned, Base):
    __tablename__ = "storage_archives"
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    manifest: Mapped[list] = mapped_column(JSON)
    plan_id: Mapped[str] = mapped_column(String(36), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FileFolder(Owned, Base):
    __tablename__ = "file_folders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "parent_id"], ["file_folders.tenant_id", "file_folders.id"]),
    )
    parent_id: Mapped[str | None] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class NativeConnection(Owned, Base):
    __tablename__ = "native_connections"
    __table_args__ = (
        UniqueConstraint("provider_user_id"),
        UniqueConstraint("tenant_id"),
        UniqueConstraint("tenant_id", "id"),
    )
    provider_user_id: Mapped[str] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class NativeLinkCode(Owned, Base):
    __tablename__ = "native_link_codes"
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class NativeFile(Owned, Base):
    __tablename__ = "native_files"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider_file_id"),
        ForeignKeyConstraint(["tenant_id", "source_id"], ["sources.tenant_id", "sources.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"], ["native_connections.tenant_id", "native_connections.id"]
        ),
    )
    provider_file_id: Mapped[str] = mapped_column(String(80))
    connection_id: Mapped[str] = mapped_column(String(36))
    source_id: Mapped[str | None] = mapped_column(String(36))
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    missing_count: Mapped[int] = mapped_column(Integer, default=0)


class FileChange(Base):
    __tablename__ = "file_changes"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    source_id: Mapped[str | None] = mapped_column(String(36))
    file_id: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(24))
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


TENANT_TABLES = [
    "knowledge_tasks",
    "knowledge_facts",
    "knowledge_decisions",
    "sources",
    "evidence",
    "claims",
    "decisions",
    "jobs",
    "pages",
    "form_drafts",
    "idempotency",
    "audit",
    "catalogs",
    "catalog_entries",
    "uploads",
    "cleanup_plans",
    "storage_archives",
    "native_connections",
    "native_link_codes",
    "native_files",
    "file_changes",
]


class AgentJob(Owned, Base):
    """Durable external semantic work; never executed by the server's job worker."""
    __tablename__ = "agent_jobs"
    __table_args__ = (UniqueConstraint("tenant_id", "source_id", "source_version"),)
    source_id: Mapped[str] = mapped_column(String(36))
    source_version: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="pending")
    credential_id: Mapped[str | None] = mapped_column(String(36))
    lease_digest: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    completion_hash: Mapped[str | None] = mapped_column(String(64))
    result_ids: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class HostTransfer(Owned, Base):
    __tablename__ = "host_transfers"
    __table_args__ = (UniqueConstraint("tenant_id", "request_id"),)
    request_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(16))
    credential_id: Mapped[str] = mapped_column(ForeignKey("credentials.id"))
    digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    upload_id: Mapped[str | None] = mapped_column(String(36))
    spec: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
