CREATE TABLE IF NOT EXISTS projects (
  id VARCHAR(64) PRIMARY KEY,
  title VARCHAR(255) NOT NULL,
  research_area VARCHAR(255) NOT NULL,
  description TEXT NOT NULL,
  project_scope_id VARCHAR(255) NOT NULL,
  learner_scope_id VARCHAR(255) NOT NULL,
  active_plan_id VARCHAR(64) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS assets (
  id VARCHAR(64) NOT NULL,
  seq BIGINT NOT NULL AUTO_INCREMENT,
  project_id VARCHAR(64) NOT NULL,
  kind VARCHAR(32) NOT NULL,
  title VARCHAR(512) NOT NULL,
  uri TEXT NOT NULL,
  checksum VARCHAR(128) NOT NULL,
  version VARCHAR(255) NULL,
  status VARCHAR(32) NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_asset_seq (seq),
  UNIQUE KEY uq_asset_location (project_id, uri(191), checksum)
);

CREATE TABLE IF NOT EXISTS assessments (
  id VARCHAR(64) PRIMARY KEY,
  project_id VARCHAR(64) NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS plans (
  id VARCHAR(64) PRIMARY KEY,
  project_id VARCHAR(64) NOT NULL,
  version INT NOT NULL,
  status VARCHAR(32) NOT NULL,
  assessment_id VARCHAR(64) NOT NULL,
  handoff_ref VARCHAR(255) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_project_plan_version (project_id, version)
);

CREATE TABLE IF NOT EXISTS tasks (
  id VARCHAR(64) PRIMARY KEY,
  plan_id VARCHAR(64) NOT NULL,
  project_id VARCHAR(64) NOT NULL,
  stage INT NOT NULL,
  title VARCHAR(512) NOT NULL,
  objective TEXT NOT NULL,
  status VARCHAR(32) NOT NULL,
  task_type VARCHAR(32) NOT NULL,
  estimated_minutes INT NOT NULL,
  position INT NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_tasks_plan (plan_id),
  INDEX idx_tasks_plan_position (plan_id, position)
);

CREATE TABLE IF NOT EXISTS task_events (
  id VARCHAR(64) NOT NULL,
  seq BIGINT NOT NULL AUTO_INCREMENT,
  project_id VARCHAR(64) NOT NULL,
  plan_id VARCHAR(64) NOT NULL,
  task_id VARCHAR(64) NOT NULL,
  event_type VARCHAR(64) NOT NULL,
  payload_json TEXT NOT NULL,
  occurred_at VARCHAR(64) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_event_seq (seq),
  INDEX idx_events_project_seq (project_id, seq),
  INDEX idx_events_task (task_id)
);
