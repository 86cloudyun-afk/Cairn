-- Apply once to the original Cairn schema. The server also performs an
-- idempotent migration automatically when opening existing databases.
PRAGMA foreign_keys = ON;
BEGIN;

ALTER TABLE projects ADD COLUMN plan TEXT NOT NULL DEFAULT '{}';

ALTER TABLE facts ADD COLUMN kind TEXT NOT NULL DEFAULT 'other';
ALTER TABLE facts ADD COLUMN summary TEXT NOT NULL DEFAULT '';
ALTER TABLE facts ADD COLUMN ip TEXT;
ALTER TABLE facts ADD COLUMN port INTEGER;
ALTER TABLE facts ADD COLUMN url TEXT;
ALTER TABLE facts ADD COLUMN confidence INTEGER;

-- Match Python's whitespace normalization while preserving source descriptions.
WITH RECURSIVE normalized(id, project_id, summary, whitespace) AS (
    SELECT id, project_id,
           description,
           char(9, 10, 11, 12, 13, 28, 29, 30, 31, 133, 160, 5760,
                8192, 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200,
                8201, 8202, 8232, 8233, 8239, 8287, 12288)
    FROM facts
    UNION ALL
    SELECT id, project_id, replace(summary, substr(whitespace, 1, 1), ' '),
           substr(whitespace, 2)
    FROM normalized
    WHERE whitespace <> ''
), collapsed(id, project_id, summary) AS (
    SELECT id, project_id, trim(summary)
    FROM normalized
    WHERE whitespace = ''
    UNION ALL
    SELECT id, project_id, replace(summary, '  ', ' ')
    FROM collapsed
    WHERE instr(summary, '  ') > 0
)
UPDATE facts
SET summary = (
    SELECT substr(collapsed.summary, 1, 120)
    FROM collapsed
    WHERE collapsed.id = facts.id
      AND collapsed.project_id = facts.project_id
      AND instr(collapsed.summary, '  ') = 0
);

ALTER TABLE intents ADD COLUMN depends_on TEXT NOT NULL DEFAULT '[]';
ALTER TABLE intents ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
ALTER TABLE intents ADD COLUMN session_key TEXT;

CREATE TABLE artifacts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    fact_id TEXT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT,
    file_path TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id),
    FOREIGN KEY (fact_id, project_id) REFERENCES facts(id, project_id) ON DELETE CASCADE
);

COMMIT;
