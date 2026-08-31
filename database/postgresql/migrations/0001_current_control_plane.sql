-- PostgreSQL laboratory schema matching the current SQLite control plane.
-- Production migration is deliberately not performed by this file.

CREATE TABLE xui_tenants (
    id text PRIMARY KEY,
    name text NOT NULL,
    canonical_host text NOT NULL UNIQUE,
    config_version bigint NOT NULL DEFAULT 1 CHECK (config_version > 0),
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE tenant_hosts (
    hostname text PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES xui_tenants(id) ON DELETE CASCADE,
    is_canonical boolean NOT NULL DEFAULT false,
    tls_status text NOT NULL DEFAULT 'pending'
        CHECK (tls_status IN ('pending', 'valid', 'failed', 'disabled'))
);

CREATE TABLE tenant_upstreams (
    id text PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES xui_tenants(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('origin', 'lb', 'vod')),
    host text NOT NULL,
    port integer NOT NULL CHECK (port BETWEEN 1 AND 65535),
    UNIQUE (tenant_id, kind, host, port)
);

CREATE UNIQUE INDEX one_origin_per_tenant
    ON tenant_upstreams (tenant_id) WHERE kind = 'origin';

CREATE TABLE edges (
    id text PRIMARY KEY,
    name text NOT NULL UNIQUE,
    ipv4 inet NOT NULL UNIQUE CHECK (family(ipv4) = 4),
    ssh_port integer NOT NULL DEFAULT 22 CHECK (ssh_port BETWEEN 1 AND 65535),
    ssh_user text NOT NULL,
    host_key_sha256 text NOT NULL,
    state text NOT NULL CHECK (state IN
        ('pending', 'bootstrapping', 'ready', 'draining', 'failed', 'disabled')),
    deployed_version text,
    last_health_at timestamptz,
    last_health_status integer,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE dns_records (
    hostname text NOT NULL REFERENCES tenant_hosts(hostname) ON DELETE CASCADE,
    record_type text NOT NULL CHECK (record_type IN ('A', 'AAAA', 'CNAME')),
    target_ip text NOT NULL,
    edge_id text REFERENCES edges(id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'draining', 'failed')),
    PRIMARY KEY (hostname, record_type, target_ip)
);

CREATE TABLE settings (
    key text PRIMARY KEY,
    value text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE deployments (
    id text PRIMARY KEY,
    state text NOT NULL CHECK (state IN
        ('queued', 'running', 'succeeded', 'failed', 'rolled_back')),
    release_id text NOT NULL,
    config_digest text NOT NULL,
    artifact_path text NOT NULL,
    error text,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at timestamptz,
    finished_at timestamptz
);
