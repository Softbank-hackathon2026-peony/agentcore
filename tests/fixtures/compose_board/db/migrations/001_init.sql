CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE auth.users (
  id            UUID PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE,
  nickname      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at    TIMESTAMPTZ
);

CREATE SCHEMA IF NOT EXISTS board;

CREATE TABLE board.posts (
  id              UUID PRIMARY KEY,
  author_id       UUID,
  author_nickname TEXT NOT NULL,
  title           TEXT NOT NULL,
  body            TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL,
  deleted_at      TIMESTAMPTZ
);

CREATE INDEX posts_author_idx ON board.posts (author_id);
