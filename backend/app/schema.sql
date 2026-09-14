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
    scope         TEXT NOT NULL,               -- drawer.code（对比抽屉，内置含 process_domain/process_stage/process_class）/ custom（不进对比抽屉）
    is_builtin    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 对比抽屉（比价页面「加工费专区」的页签来源；内置 3 个不可改删）
CREATE TABLE IF NOT EXISTS drawer (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    is_builtin  INTEGER NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
INSERT OR IGNORE INTO drawer (code, name, is_builtin, sort_order) VALUES
    ('process_domain', '工艺域', 1, 10),
    ('process_stage', '工艺阶段', 1, 20),
    ('process_class', '工艺类别', 1, 30);

CREATE TABLE IF NOT EXISTS supplier (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    alias       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 项目 = 公司内部的一个具体 SKU 产品；报价单识别出的项目名按名称归一化后绑定到这里
CREATE TABLE IF NOT EXISTS project (
    code         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    category_code TEXT REFERENCES category(code),
    remark       TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_name ON project(name);

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
    project_code             TEXT REFERENCES project(code),  -- 绑定的项目（SKU），未匹配为 null
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
    other_info               TEXT,            -- 其它信息（Markdown）：解析中识别到但不属于任何结构化字段的内容，供 AI 分析补充
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
CREATE INDEX IF NOT EXISTS idx_quote_supplier ON quote(supplier_code);
CREATE INDEX IF NOT EXISTS idx_quote_project ON quote(project_code);

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
    stage       TEXT NOT NULL,               -- dedup/ingest/layout/match/validate/persist/ai_analysis
    action      TEXT NOT NULL,
    detail      TEXT,                        -- JSON：输入输出摘要、token、耗时等
    is_llm_call INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_parse_log_quote ON parse_log(quote_id);

-- AI 分析（LLM 生成结构化对比表格 + 数字回检）；按输入指纹版本化，同指纹复用
CREATE TABLE IF NOT EXISTS ai_analysis (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL REFERENCES comparison_task(id),
    input_signature TEXT NOT NULL,             -- 结构化输入 + prompt 版本的 sha256
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'completed', 'failed')),
    content         TEXT,                      -- JSON：AI 分析表格结构
    check_status    TEXT NOT NULL DEFAULT 'unchecked'
                    CHECK (check_status IN ('pass', 'mismatch', 'unchecked')),
    check_detail    TEXT,                      -- JSON：数字回检明细
    error           TEXT,                      -- failed 时的错误摘要
    model           TEXT,
    tokens          INTEGER,
    elapsed_ms      INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_ai_analysis_task ON ai_analysis(task_id, id DESC);

-- 旧版「AI 建议」模块已由「AI 分析」替代
DROP TABLE IF EXISTS ai_summary;

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

-- 对象存储登记：每次上传/接入各留一条（同一内容多次上传共用一份字节，文件名按次留存）
CREATE TABLE IF NOT EXISTS stored_object (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256        TEXT NOT NULL,
    object_key    TEXT NOT NULL,          -- 内容寻址 key：objects/<sha[:2]>/<sha><ext>
    backend       TEXT NOT NULL DEFAULT 'local',
    size_bytes    INTEGER,
    content_type  TEXT,
    original_name TEXT NOT NULL,          -- 源文件名称（上传时用户给的名字）
    task_id       INTEGER REFERENCES comparison_task(id),
    quote_id      INTEGER REFERENCES quote(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_stored_object_quote ON stored_object(quote_id);
CREATE INDEX IF NOT EXISTS idx_stored_object_sha ON stored_object(sha256);
