-- Future node/LB model. It coexists with `edges` until a separately approved
-- cutover removes that compatibility table.

CREATE TABLE nodes (
    id text PRIMARY KEY,
    name text NOT NULL UNIQUE,
    ipv4 inet NOT NULL UNIQUE CHECK (family(ipv4) = 4),
    role text NOT NULL CHECK (role IN ('control_plane', 'edge', 'load_balancer')),
    state text NOT NULL CHECK (state IN
        ('candidate', 'ready', 'draining', 'active', 'standby', 'fenced', 'failed', 'disabled')),
    release_id text,
    node_config_digest text,
    capacity_json jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(capacity_json) = 'object'),
    lease_id uuid,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE load_balancers (
    id text PRIMARY KEY,
    node_id text NOT NULL UNIQUE REFERENCES nodes(id) ON DELETE RESTRICT,
    mode text NOT NULL DEFAULT 'active_standby'
        CHECK (mode IN ('active_standby', 'active_active')),
    state text NOT NULL DEFAULT 'candidate'
        CHECK (state IN ('candidate', 'standby', 'active', 'draining', 'fenced', 'failed', 'disabled')),
    public_endpoint text,
    config_version bigint NOT NULL DEFAULT 1 CHECK (config_version > 0),
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- The first production version is active/standby. Remove this index only in a
-- separately reviewed active/active migration after proven soak and failover.
CREATE UNIQUE INDEX one_active_load_balancer
    ON load_balancers ((state)) WHERE state = 'active';

CREATE TABLE lb_backends (
    load_balancer_id text NOT NULL REFERENCES load_balancers(id) ON DELETE CASCADE,
    edge_node_id text NOT NULL REFERENCES nodes(id) ON DELETE RESTRICT,
    weight integer NOT NULL DEFAULT 100 CHECK (weight BETWEEN 0 AND 256),
    state text NOT NULL DEFAULT 'ready'
        CHECK (state IN ('ready', 'draining', 'failed', 'disabled')),
    last_health_at timestamptz,
    PRIMARY KEY (load_balancer_id, edge_node_id),
    CHECK (load_balancer_id <> edge_node_id)
);

CREATE TABLE promotion_locks (
    service_id text PRIMARY KEY,
    holder_node_id text NOT NULL REFERENCES nodes(id) ON DELETE RESTRICT,
    lease_id uuid NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE node_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    node_id text NOT NULL REFERENCES nodes(id) ON DELETE RESTRICT,
    event_type text NOT NULL,
    operator text NOT NULL,
    reason text NOT NULL,
    payload_sanitized jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(payload_sanitized) = 'object'),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE OR REPLACE FUNCTION cdnmnus_validate_lb_role()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM nodes WHERE id = NEW.node_id AND role = 'load_balancer'
    ) THEN
        RAISE EXCEPTION 'load_balancer node % must have load_balancer role', NEW.node_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER validate_load_balancer_role
BEFORE INSERT OR UPDATE OF node_id ON load_balancers
FOR EACH ROW EXECUTE FUNCTION cdnmnus_validate_lb_role();

CREATE OR REPLACE FUNCTION cdnmnus_validate_backend_role()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM nodes WHERE id = NEW.edge_node_id AND role = 'edge'
    ) THEN
        RAISE EXCEPTION 'backend node % must have edge role', NEW.edge_node_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER validate_backend_role
BEFORE INSERT OR UPDATE OF edge_node_id ON lb_backends
FOR EACH ROW EXECUTE FUNCTION cdnmnus_validate_backend_role();

CREATE OR REPLACE FUNCTION cdnmnus_validate_active_lb_lease()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state = 'active' AND NOT EXISTS (
        SELECT 1
          FROM promotion_locks lock
         WHERE lock.service_id = NEW.id
           AND lock.holder_node_id = NEW.node_id
           AND lock.expires_at > CURRENT_TIMESTAMP
           AND lock.lease_id = (SELECT lease_id FROM nodes WHERE id = NEW.node_id)
    ) THEN
        RAISE EXCEPTION 'active load balancer requires a valid promotion lease';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER validate_active_load_balancer_lease
BEFORE INSERT OR UPDATE OF state ON load_balancers
FOR EACH ROW EXECUTE FUNCTION cdnmnus_validate_active_lb_lease();

CREATE OR REPLACE FUNCTION cdnmnus_prevent_fencing_token_regression()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.fencing_token <= OLD.fencing_token AND NEW.lease_id <> OLD.lease_id THEN
        RAISE EXCEPTION 'new lease fencing token must increase';
    END IF;
    IF NEW.fencing_token <> OLD.fencing_token AND NEW.lease_id = OLD.lease_id THEN
        RAISE EXCEPTION 'lease renewal cannot change fencing token';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER prevent_fencing_token_regression
BEFORE UPDATE ON promotion_locks
FOR EACH ROW EXECUTE FUNCTION cdnmnus_prevent_fencing_token_regression();
