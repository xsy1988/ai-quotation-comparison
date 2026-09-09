-- 采购报价对比Agent 数据库 Schema
-- 主数据表（§4.1）+ 业务数据表（§4.2），SQLite 方言

-- ========== 主数据表 ==========

CREATE TABLE IF NOT EXISTS category (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS process_domain (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS process_stage (
    name        TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS process_class (
    name        TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS atom (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    domain_code TEXT NOT NULL REFERENCES process_domain(code),
    stage_name  TEXT NOT NULL REFERENCES process_stage(name),
    class_name  TEXT NOT NULL REFERENCES process_class(name),
    remark      TEXT,
    is_fallback INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_atom_domain ON atom(domain_code);
CREATE INDEX IF NOT EXISTS idx_atom_stage ON atom(stage_name);
CREATE INDEX IF NOT EXISTS idx_atom_class ON atom(class_name);

CREATE TABLE IF NOT EXISTS atom_alias (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    atom_code   TEXT NOT NULL REFERENCES atom(code),
    alias_text  TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'initial'
                CHECK (source IN ('initial', 'manual_feedback', 'new_process')),
    hit_count   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (atom_code, alias_text)
);
CREATE INDEX IF NOT EXISTS idx_alias_text ON atom_alias(alias_text);

CREATE TABLE IF NOT EXISTS atom_category (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    atom_code    TEXT NOT NULL REFERENCES atom(code),
    category_code TEXT NOT NULL REFERENCES category(code),
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (atom_code, category_code)
);

CREATE TABLE IF NOT EXISTS dim_group (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    group_code    TEXT NOT NULL UNIQUE,
    group_name    TEXT NOT NULL,
    parent_code   TEXT REFERENCES dim_group(group_code),
    member_atoms  TEXT NOT NULL DEFAULT '[]',  -- JSON 数组：原子编码列表
    scope         TEXT NOT NULL,               -- process_domain / process_stage / process_class / custom
    is_builtin    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS supplier (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    alias       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ========== 业务数据表 ==========

CREATE TABLE IF NOT EXISTS comparison_task (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    category_code TEXT REFERENCES category(code),
    status       TEXT NOT NULL DEFAULT 'created'
                 CHECK (status IN ('created', 'parsing', 'parsed', 'failed', 'reviewed')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS quote (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                  INTEGER NOT NULL REFERENCES comparison_task(id),
    supplier_code            TEXT REFERENCES supplier(code),
    supplier_name            TEXT,            -- 冗余：新供应商 code 为 null 时对比仍显示名字
    category_code            TEXT REFERENCES category(code),
    basic_info               TEXT,            -- JSON：项目/零件/币种/MOQ 等基本信息
    final_unit_price_taxed   REAL,
    untaxed_total            REAL,
    tax_amount               REAL,
    discount                 REAL,
    materials_total          REAL,
    processing_total         REAL,
    inspection_total         REAL,
    packaging_transport_total REAL,
    sga_tax_total            REAL,
    other_total              REAL,
    tooling_total            REAL,
    raw_json_path            TEXT,
    file_hash                TEXT,            -- 查重门禁用
    flags                    TEXT,            -- JSON 数组：校验徽标（calc_abnormal/cross_validation_conflict/low_confidence/unmatched/new_process/category_doubt）
    parse_status             TEXT NOT NULL DEFAULT 'pending'
                             CHECK (parse_status IN ('pending', 'parsed', 'in_comparison', 'reviewed', 'failed')),
    calc_check               TEXT CHECK (calc_check IN ('pass', 'fail')),
    created_at               TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_quote_task ON quote(task_id);

CREATE TABLE IF NOT EXISTS quote_line (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id        INTEGER NOT NULL REFERENCES quote(id),
    module          TEXT NOT NULL
                    CHECK (module IN ('materials', 'processing', 'inspection',
                                      'packaging_transport', 'sga_tax', 'other')),
    item_name       TEXT NOT NULL,
    item_type       TEXT,                    -- 损管利税等条目子类型（损耗/管理费/利润/税费）
    amount          REAL,
    unit            TEXT DEFAULT '元/pcs',
    rate            REAL,                    -- 按百分比报价时的原费率
    atom_code       TEXT REFERENCES atom(code),
    is_new_process  INTEGER NOT NULL DEFAULT 0,
    bundle_flag     INTEGER NOT NULL DEFAULT 0,
    candidate_atoms TEXT,                    -- JSON 数组：候选原子编码
    fingerprint     TEXT,                    -- 打包项组合指纹
    confidence      TEXT CHECK (confidence IN ('high', 'medium', 'low')),
    match_path      TEXT,                    -- L1_alias / L2_llm / L3_none / manual
    cross_check     TEXT,                    -- JSON：交叉验证双值保留 {"script_value":x,"llm_value":y}
    confirm_status  TEXT NOT NULL DEFAULT 'unconfirmed'
                    CHECK (confirm_status IN ('unconfirmed', 'confirmed', 'corrected')),
    note            TEXT,
    evidence        TEXT,                    -- JSON：原文出处（文件+位置+片段）
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_quote_line_quote ON quote_line(quote_id);
CREATE INDEX IF NOT EXISTS idx_quote_line_atom ON quote_line(atom_code);

CREATE TABLE IF NOT EXISTS tooling_line (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id     INTEGER NOT NULL REFERENCES quote(id),
    tooling_type TEXT NOT NULL CHECK (tooling_type IN ('mold', 'fixture', 'stencil')),
    item_name    TEXT NOT NULL,
    amount       REAL,
    cavity_count INTEGER,
    lifespan     INTEGER,                    -- 模穴寿命（次数）
    note         TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_tooling_line_quote ON tooling_line(quote_id);

CREATE TABLE IF NOT EXISTS new_atom_suggestion (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_line_id        INTEGER REFERENCES quote_line(id),
    source_text          TEXT NOT NULL,      -- 条目原文
    suggested_name       TEXT,
    suggested_domain_code TEXT REFERENCES process_domain(code),
    suggested_stage_name TEXT REFERENCES process_stage(name),
    occurrence_count     INTEGER NOT NULL DEFAULT 1,
    status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'created', 'merged', 'ignored')),
    created_at           TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS unmatched_term (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    term_text        TEXT NOT NULL,
    context          TEXT,                   -- 原文上下文片段
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'resolved', 'ignored')),
    created_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (term_text)
);

CREATE TABLE IF NOT EXISTS parse_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id    INTEGER REFERENCES quote(id),
    task_id     INTEGER REFERENCES comparison_task(id),
    stage       TEXT NOT NULL,               -- dedup/ingest/layout/match/validate/persist/ai_summary
    action      TEXT NOT NULL,
    detail      TEXT,                        -- JSON：输入输出摘要、token、耗时等
    is_llm_call INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_parse_log_quote ON parse_log(quote_id);

-- AI 综合建议（LLM 生成 + 数字回检）
CREATE TABLE IF NOT EXISTS ai_summary (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL REFERENCES comparison_task(id),
    content      TEXT NOT NULL,                -- AI 建议 markdown 文本
    check_status TEXT NOT NULL DEFAULT 'unchecked'
                 CHECK (check_status IN ('pass', 'mismatch')),
    check_detail TEXT,                         -- JSON：数字回检明细
    model        TEXT,
    tokens       INTEGER,
    elapsed_ms   INTEGER,
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_ai_summary_task ON ai_summary(task_id);

-- 源文件登记与查重（ingest 前置门禁）
CREATE TABLE IF NOT EXISTS source_file (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256        TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL,
    file_type     TEXT NOT NULL,          -- xlsx / docx / pdf ...
    archived_path TEXT NOT NULL,
    ir_path       TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
