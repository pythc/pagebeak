"""Versioned knowledge candidates and leased semantic tasks. Existing data is preserved."""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.execute('\nCREATE TABLE knowledge_tasks (\n\tsource_id VARCHAR(36) NOT NULL, \n\tgeneration_id VARCHAR(36) NOT NULL, \n\tsource_hash VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(24) NOT NULL, \n\trevision INTEGER NOT NULL, \n\towner_id VARCHAR(36), \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tresult JSON NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(36) NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (tenant_id, source_id, generation_id), \n\tFOREIGN KEY(tenant_id, source_id) REFERENCES sources (tenant_id, id), \n\tFOREIGN KEY(tenant_id) REFERENCES tenants (id)\n)\n\n')
    op.execute('CREATE INDEX ix_knowledge_tasks_source_id ON knowledge_tasks (source_id)')
    op.execute('CREATE INDEX ix_knowledge_tasks_tenant_id ON knowledge_tasks (tenant_id)')
    op.execute('ALTER TABLE public.knowledge_tasks ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE public.knowledge_tasks FORCE ROW LEVEL SECURITY')
    op.execute("CREATE POLICY tenant_scope ON public.knowledge_tasks USING (tenant_id = current_setting('akb.tenant_id', true)) WITH CHECK (tenant_id = current_setting('akb.tenant_id', true))")
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON public.knowledge_tasks TO akb_app, akb_worker')
    op.execute('\nCREATE TABLE knowledge_facts (\n\tsource_id VARCHAR(36), \n\tevidence_id VARCHAR(36), \n\tgeneration_id VARCHAR(36), \n\tdedup_key VARCHAR(64) NOT NULL, \n\tkind VARCHAR(24) NOT NULL, \n\tsubject VARCHAR(200) NOT NULL, \n\tsubject_scope VARCHAR(16) NOT NULL, \n\tentity_key VARCHAR(250) NOT NULL, \n\tfield VARCHAR(40) NOT NULL, \n\tvalue TEXT NOT NULL, \n\tpolarity VARCHAR(16) NOT NULL, \n\tvalid_from VARCHAR(10), \n\tvalid_to VARCHAR(10), \n\tquote TEXT NOT NULL, \n\tbasis VARCHAR(24) NOT NULL, \n\tstatus VARCHAR(24) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(36) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(tenant_id, source_id) REFERENCES sources (tenant_id, id), \n\tFOREIGN KEY(tenant_id, evidence_id) REFERENCES evidence (tenant_id, id), \n\tUNIQUE (tenant_id, dedup_key), \n\tFOREIGN KEY(tenant_id) REFERENCES tenants (id)\n)\n\n')
    op.execute('CREATE INDEX ix_knowledge_facts_source_id ON knowledge_facts (source_id)')
    op.execute('CREATE INDEX ix_knowledge_facts_tenant_id ON knowledge_facts (tenant_id)')
    op.execute('ALTER TABLE public.knowledge_facts ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE public.knowledge_facts FORCE ROW LEVEL SECURITY')
    op.execute("CREATE POLICY tenant_scope ON public.knowledge_facts USING (tenant_id = current_setting('akb.tenant_id', true)) WITH CHECK (tenant_id = current_setting('akb.tenant_id', true))")
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON public.knowledge_facts TO akb_app, akb_worker')
    op.execute('\nCREATE TABLE knowledge_decisions (\n\tfact_id VARCHAR(36) NOT NULL, \n\taction VARCHAR(24) NOT NULL, \n\tdetail JSON NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\ttenant_id VARCHAR(36) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(tenant_id) REFERENCES tenants (id)\n)\n\n')
    op.execute('CREATE INDEX ix_knowledge_decisions_tenant_id ON knowledge_decisions (tenant_id)')
    op.execute('ALTER TABLE public.knowledge_decisions ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE public.knowledge_decisions FORCE ROW LEVEL SECURITY')
    op.execute("CREATE POLICY tenant_scope ON public.knowledge_decisions USING (tenant_id = current_setting('akb.tenant_id', true)) WITH CHECK (tenant_id = current_setting('akb.tenant_id', true))")
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON public.knowledge_decisions TO akb_app, akb_worker')
    op.execute("INSERT INTO public.jobs (id, tenant_id, kind, source_revision, status, fencing_token, attempts, created_at, result) SELECT gen_random_uuid()::text, c.tenant_id, 'organize', 0, 'queued', 0, 0, clock_timestamp(), '{}'::json FROM public.catalogs c WHERE c.enabled AND NOT EXISTS (SELECT 1 FROM public.jobs j WHERE j.tenant_id=c.tenant_id AND j.kind='organize' AND j.status IN ('queued','running'))")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; use a reviewed forward migration.")
