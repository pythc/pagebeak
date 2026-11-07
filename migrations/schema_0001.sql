-- Frozen initial schema; do not regenerate after release.

CREATE TABLE users (
	id VARCHAR(36) NOT NULL, 
	email VARCHAR(254) NOT NULL, 
	display_name VARCHAR(80) NOT NULL, 
	password_hash TEXT NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (email), 
	UNIQUE (tenant_id)
);

CREATE TABLE tenants (
	id VARCHAR(36) NOT NULL, 
	revision INTEGER NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE credentials (
	id VARCHAR(36) NOT NULL, 
	digest VARCHAR(64) NOT NULL, 
	user_id VARCHAR(36) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	label VARCHAR(80) NOT NULL, 
	scopes JSON NOT NULL, 
	csrf VARCHAR(64) NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	revoked BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (digest), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE sources (
	title VARCHAR(255) NOT NULL, 
	filename VARCHAR(255) NOT NULL, 
	media_type VARCHAR(120) NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	object_key VARCHAR(200) NOT NULL, 
	size INTEGER NOT NULL, 
	subject_scope VARCHAR(16) NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	coverage VARCHAR(24) NOT NULL, 
	error TEXT, 
	category VARCHAR(40) NOT NULL, 
	revision INTEGER NOT NULL, 
	generation_id VARCHAR(36), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (tenant_id, id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);

CREATE INDEX ix_sources_tenant_id ON sources (tenant_id);

CREATE TABLE decisions (
	claim_id VARCHAR(36) NOT NULL, 
	action VARCHAR(24) NOT NULL, 
	previous_value TEXT NOT NULL, 
	new_value TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);

CREATE INDEX ix_decisions_tenant_id ON decisions (tenant_id);

CREATE TABLE pages (
	title VARCHAR(200) NOT NULL, 
	content TEXT NOT NULL, 
	evidence_ids JSON NOT NULL, 
	origin_kind VARCHAR(20) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);

CREATE INDEX ix_pages_tenant_id ON pages (tenant_id);

CREATE TABLE form_drafts (
	title VARCHAR(200) NOT NULL, 
	fields JSON NOT NULL, 
	answers JSON NOT NULL, 
	snapshot_revision INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);

CREATE INDEX ix_form_drafts_tenant_id ON form_drafts (tenant_id);

CREATE TABLE idempotency (
	operation VARCHAR(100) NOT NULL, 
	key VARCHAR(120) NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	resource_id VARCHAR(36) NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (tenant_id, operation, key), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);

CREATE INDEX ix_idempotency_tenant_id ON idempotency (tenant_id);

CREATE TABLE audit (
	action VARCHAR(80) NOT NULL, 
	resource_id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);

CREATE INDEX ix_audit_tenant_id ON audit (tenant_id);

CREATE TABLE evidence (
	source_id VARCHAR(36) NOT NULL, 
	generation_id VARCHAR(36) NOT NULL, 
	ordinal INTEGER NOT NULL, 
	locator JSON NOT NULL, 
	text TEXT NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (tenant_id, id), 
	FOREIGN KEY(tenant_id, source_id) REFERENCES sources (tenant_id, id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);

CREATE INDEX ix_evidence_source_id ON evidence (source_id);

CREATE INDEX ix_evidence_tenant_id ON evidence (tenant_id);

CREATE TABLE jobs (
	source_id VARCHAR(36) NOT NULL, 
	kind VARCHAR(20) NOT NULL, 
	source_revision INTEGER NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	fencing_token INTEGER NOT NULL, 
	attempts INTEGER NOT NULL, 
	lease_until TIMESTAMP WITH TIME ZONE, 
	worker_id VARCHAR(36), 
	error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id, source_id) REFERENCES sources (tenant_id, id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);

CREATE INDEX ix_jobs_source_id ON jobs (source_id);

CREATE INDEX ix_jobs_status ON jobs (status);

CREATE INDEX ix_jobs_tenant_id ON jobs (tenant_id);

CREATE TABLE claims (
	source_id VARCHAR(36), 
	evidence_id VARCHAR(36), 
	field VARCHAR(40) NOT NULL, 
	value TEXT NOT NULL, 
	quote TEXT NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	basis VARCHAR(24) NOT NULL, 
	revision INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id, source_id) REFERENCES sources (tenant_id, id), 
	FOREIGN KEY(tenant_id, evidence_id) REFERENCES evidence (tenant_id, id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);

CREATE INDEX ix_claims_tenant_id ON claims (tenant_id);

CREATE INDEX ix_claims_source_id ON claims (source_id);
