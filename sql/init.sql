-- ============================================================================
-- Flight Smart Booking POC (LastSeat) - Database Initialization Script
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Table of Normalized Flight Deals (Multi-Source Ingestion)
CREATE TABLE IF NOT EXISTS flight_deals (
    deal_id VARCHAR(128) PRIMARY KEY,
    source VARCHAR(32) NOT NULL,               -- 'GDS_POLLER' | 'PARTNER_WEBHOOK'
    flight_number VARCHAR(32) NOT NULL,
    airline_code VARCHAR(8) NOT NULL,
    origin VARCHAR(3) NOT NULL,                -- IATA 3-letter code (e.g. CDG)
    destination VARCHAR(3) NOT NULL,           -- IATA 3-letter code (e.g. JFK)
    departure_at TIMESTAMPTZ NOT NULL,
    arrival_at TIMESTAMPTZ NOT NULL,
    price NUMERIC(10, 2) NOT NULL CHECK (price >= 0),
    currency VARCHAR(3) NOT NULL DEFAULT 'EUR',
    cabin_class VARCHAR(16) NOT NULL DEFAULT 'ECONOMY',
    seats_available INTEGER NOT NULL CHECK (seats_available >= 0),
    expires_at TIMESTAMPTZ NOT NULL,
    raw_metadata JSONB DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE', -- 'ACTIVE' | 'SOLD_OUT' | 'EXPIRED'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_flight_deals_route_status
    ON flight_deals (origin, destination, status, price);

-- 2. Table of Autonomous Agent Rules configured by Users
CREATE TABLE IF NOT EXISTS agent_rules (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(64) NOT NULL,
    origin VARCHAR(3) NOT NULL,
    destination VARCHAR(3) NULL,               -- NULL means "Any destination" (wildcard)
    max_price NUMERIC(10, 2) NOT NULL CHECK (max_price > 0),
    currency VARCHAR(3) NOT NULL DEFAULT 'EUR',
    preferred_cabin VARCHAR(16) NOT NULL DEFAULT 'ECONOMY',
    earliest_departure TIMESTAMPTZ NULL,
    latest_departure TIMESTAMPTZ NULL,
    auto_purchase_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    passenger_info JSONB NOT NULL DEFAULT '{"first_name": "Jean", "last_name": "Dupont", "passport": "FR99887766"}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_rules_matching
    ON agent_rules (origin, auto_purchase_enabled, max_price);

-- 3. Table of Autonomous Bookings & Saga State Machine
CREATE TABLE IF NOT EXISTS booking_transactions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    idempotency_key VARCHAR(160) NOT NULL UNIQUE, -- Format: "{deal_id}:{user_id}"
    deal_id VARCHAR(128) NOT NULL REFERENCES flight_deals(deal_id) ON DELETE RESTRICT,
    rule_id UUID NOT NULL REFERENCES agent_rules(id) ON DELETE RESTRICT,
    user_id VARCHAR(64) NOT NULL,
    amount_paid NUMERIC(10, 2) NOT NULL,
    currency VARCHAR(3) NOT NULL DEFAULT 'EUR',
    status VARCHAR(32) NOT NULL,               -- 'PENDING_GDS' | 'CONFIRMED' | 'COMPENSATED_FAILED'
    pnr_reference VARCHAR(32) NULL,
    failure_reason TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_booking_deal_user UNIQUE (deal_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_booking_transactions_user
    ON booking_transactions (user_id, status);

-- ============================================================================
-- Seed Data: Default Autonomous Buying Rules for Immediate Demonstration
-- ============================================================================
INSERT INTO agent_rules (id, user_id, origin, destination, max_price, currency, preferred_cabin, auto_purchase_enabled, passenger_info)
VALUES
    (
        'a1111111-1111-4111-8111-111111111111',
        'usr_alice_01',
        'CDG',
        'JFK',
        290.00,
        'EUR',
        'ECONOMY',
        TRUE,
        '{"first_name": "Alice", "last_name": "Martin", "email": "alice@example.com", "passport": "FR10293847"}'::jsonb
    ),
    (
        'b2222222-2222-4222-8222-222222222222',
        'usr_bob_02',
        'CDG',
        'JFK',
        320.00,
        'EUR',
        'ECONOMY',
        TRUE,
        '{"first_name": "Bob", "last_name": "Bernard", "email": "bob@example.com", "passport": "FR55667788"}'::jsonb
    ),
    (
        'c3333333-3333-4333-8333-333333333333',
        'usr_claire_nomad',
        'ORY',
        NULL,
        150.00,
        'EUR',
        'ECONOMY',
        TRUE,
        '{"first_name": "Claire", "last_name": "Dubois", "email": "claire@example.com", "passport": "FR99001122"}'::jsonb
    ),
    (
        'd4444444-4444-4444-8444-444444444444',
        'usr_karim_cmn',
        'CMN',
        'ORY',
        55.00,
        'EUR',
        'ECONOMY',
        TRUE,
        '{"first_name": "Karim", "last_name": "Benali", "email": "karim@example.com", "passport": "MA77889900"}'::jsonb
    )
ON CONFLICT (id) DO NOTHING;
