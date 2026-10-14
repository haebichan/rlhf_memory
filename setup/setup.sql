-- RLHF-inspired agent memory: full setup, exported from the live demo objects.
--
-- Run with the Snowflake CLI from the repo root so the PUT paths resolve:
--     snow sql -f setup/setup.sql -c <connection>
-- (In Snowsight, skip the PUT lines and upload setup/data/*.csv to @RLHF_SEED instead.)
--
-- Objects live in HAEBI_DEMO.RLHF_MEMORY and use warehouse COMPUTE_WH. Find/replace both to use your own.
-- Requires a role that can create agents and Cortex Search services, plus SNOWFLAKE.CORTEX_USER.

CREATE DATABASE IF NOT EXISTS HAEBI_DEMO;
CREATE OR REPLACE SCHEMA HAEBI_DEMO.RLHF_MEMORY
  COMMENT = 'Feedback-driven agent memory (RLHF-like) research demo';
USE SCHEMA HAEBI_DEMO.RLHF_MEMORY;

-- ---------------------------------------------------------------- 1. Mock retail data
-- Planted traps: channel = 'INTERNAL' rows are test orders, revenue should be net
-- (gross - discount - returned), and fiscal years start Feb 1 (see FISCAL_CALENDAR).

CREATE OR REPLACE TABLE STORES (
  STORE_ID NUMBER(2,0), STORE_NAME VARCHAR, REGION VARCHAR, STATE VARCHAR(2));

CREATE OR REPLACE TABLE PRODUCTS (
  PRODUCT_ID NUMBER(3,0), PRODUCT_NAME VARCHAR, CATEGORY VARCHAR, LIST_PRICE NUMBER(3,0));

CREATE OR REPLACE TABLE ORDERS (
  ORDER_ID NUMBER(11,0), ORDER_DATE DATE, STORE_ID NUMBER(2,0), PRODUCT_ID NUMBER(38,0),
  CHANNEL VARCHAR(8), QUANTITY NUMBER(3,0), GROSS_AMOUNT NUMBER(6,0),
  DISCOUNT_AMOUNT NUMBER(15,2), RETURNED_AMOUNT NUMBER(17,2));

CREATE OR REPLACE TABLE FISCAL_CALENDAR (
  CALENDAR_DATE DATE, FISCAL_YEAR NUMBER(4,0), FISCAL_QUARTER VARCHAR, FISCAL_PERIOD VARCHAR);

CREATE OR REPLACE STAGE RLHF_SEED
  FILE_FORMAT = (TYPE = CSV PARSE_HEADER = TRUE FIELD_OPTIONALLY_ENCLOSED_BY = '"' NULL_IF = (''));

PUT file://setup/data/*.csv @RLHF_SEED AUTO_COMPRESS = FALSE OVERWRITE = TRUE;

COPY INTO STORES          FROM @RLHF_SEED/stores.csv          MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE;
COPY INTO PRODUCTS        FROM @RLHF_SEED/products.csv        MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE;
COPY INTO ORDERS          FROM @RLHF_SEED/orders.csv          MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE;
COPY INTO FISCAL_CALENDAR FROM @RLHF_SEED/fiscal_calendar.csv MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE;

-- ---------------------------------------------------------------- 2. Naive semantic view
-- Intentionally missing the business rules above, so the agent starts out wrong.

CREATE OR REPLACE SEMANTIC VIEW RETAIL_SALES_SV
  TABLES (
    ORDERS PRIMARY KEY (ORDER_ID) COMMENT = 'One row per order line',
    STORES PRIMARY KEY (STORE_ID) COMMENT = 'Retail store locations',
    PRODUCTS PRIMARY KEY (PRODUCT_ID) COMMENT = 'Product catalog'
  )
  RELATIONSHIPS (
    ORDERS_TO_PRODUCTS AS ORDERS(PRODUCT_ID) REFERENCES PRODUCTS(PRODUCT_ID),
    ORDERS_TO_STORES AS ORDERS(STORE_ID) REFERENCES STORES(STORE_ID)
  )
  FACTS (
    ORDERS.GROSS_AMOUNT AS gross_amount COMMENT = 'Order line amount at list price',
    ORDERS.DISCOUNT_AMOUNT AS discount_amount COMMENT = 'Discount applied',
    ORDERS.RETURNED_AMOUNT AS returned_amount COMMENT = 'Amount refunded for returns',
    ORDERS.QUANTITY AS quantity COMMENT = 'Units sold'
  )
  DIMENSIONS (
    ORDERS.ORDER_DATE AS order_date COMMENT = 'Date of the order',
    ORDERS.CHANNEL AS channel COMMENT = 'Sales channel',
    STORES.STORE_NAME AS store_name COMMENT = 'Store name',
    STORES.REGION AS region COMMENT = 'Store region',
    STORES.STATE AS state COMMENT = 'Store state',
    PRODUCTS.PRODUCT_NAME AS product_name COMMENT = 'Product name',
    PRODUCTS.CATEGORY AS category COMMENT = 'Product category'
  )
  METRICS (
    ORDERS.TOTAL_REVENUE AS SUM(orders.gross_amount) COMMENT = 'Total revenue',
    ORDERS.TOTAL_DISCOUNTS AS SUM(orders.discount_amount) COMMENT = 'Total discounts',
    ORDERS.TOTAL_RETURNS AS SUM(orders.returned_amount) COMMENT = 'Total returns',
    ORDERS.UNITS_SOLD AS SUM(orders.quantity) COMMENT = 'Total units sold',
    ORDERS.ORDER_COUNT AS COUNT(orders.order_id) COMMENT = 'Number of orders'
  )
  COMMENT = 'Retail sales (naive baseline - intentionally missing business definitions)';

-- ---------------------------------------------------------------- 3. Review queue and lesson memory

CREATE OR REPLACE TABLE REVIEW_QUEUE (
  CASE_ID NUMBER AUTOINCREMENT START 1 INCREMENT 1 NOORDER,
  REQUEST_ID VARCHAR, THREAD_ID VARCHAR, FEEDBACK_USER VARCHAR, FEEDBACK_POSITIVE BOOLEAN,
  FEEDBACK_MESSAGE VARCHAR, USER_QUESTION VARCHAR, AGENT_ANSWER VARCHAR, GENERATED_SQL VARCHAR,
  CASE_FILE VARIANT,
  STATUS VARCHAR DEFAULT 'PENDING',   -- PENDING | APPROVED | REJECTED
  REVIEWER VARCHAR, REVIEWED_AT TIMESTAMP_LTZ,
  CREATED_AT TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP());

CREATE OR REPLACE TABLE LESSONS (
  LESSON_ID NUMBER AUTOINCREMENT START 1 INCREMENT 1 NOORDER,
  SOURCE_CASE_ID NUMBER, EXAMPLE_QUESTION VARCHAR, ISSUE VARCHAR, GUIDANCE VARCHAR, SCOPE VARCHAR,
  TIER VARCHAR,                       -- TIER2_MEMORY | TIER1_SOURCE_OF_TRUTH
  STATUS VARCHAR DEFAULT 'APPROVED',  -- APPROVED | RETIRED
  APPROVED_BY VARCHAR,
  UPDATED_AT TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP())
  COMMENT = 'Human-approved lessons distilled from thumbs-down feedback';

-- Optional: load the three lessons approved in the demo. Skip this to run the loop from empty memory.
COPY INTO LESSONS FROM @RLHF_SEED/lessons.csv MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE;

-- Only APPROVED rows are indexed; approvals and retirements reach the agent within TARGET_LAG.
CREATE OR REPLACE CORTEX SEARCH SERVICE LESSONS_SEARCH
  ON LESSON_TEXT
  ATTRIBUTES SCOPE, TIER
  WAREHOUSE = COMPUTE_WH
  TARGET_LAG = '1 minute'
  COMMENT = 'Long-term agent memory: human-approved lessons from user feedback'
  AS
    SELECT lesson_id, scope, tier, guidance, example_question,
           'Question: ' || example_question || '\nIssue: ' || issue || '\nGuidance: ' || guidance AS lesson_text
    FROM LESSONS
    WHERE status = 'APPROVED';

-- ---------------------------------------------------------------- 4. Agents

CREATE OR REPLACE AGENT RLHF_MEMORY_AGENT
  COMMENT = 'Agent with feedback-driven long-term memory (RLHF-like) via Cortex Search over approved lessons'
  PROFILE = '{"display_name": "RLHF Memory Agent"}'
  FROM SPECIFICATION $$
models:
  orchestration: auto
orchestration:
  budget: {seconds: 900, tokens: 400000}
instructions:
  response: "Answer concisely with the key number(s). State the definitions and date range used. If you applied a past lesson, add one line: 'Applied learned guidance: <short summary>'."
  orchestration: "You are a retail sales analytics assistant. For EVERY user question, FIRST call the lessons_memory tool with the user's question to retrieve past human-approved corrections. If relevant lessons are returned, you MUST follow their guidance (metric definitions, date interpretations, filters) when calling query_sales, and it overrides default interpretations. Then call query_sales to answer."
tools:
  - tool_spec:
      type: cortex_search
      name: lessons_memory
      description: "Long-term memory of human-approved corrections from past user feedback. Returns lessons with guidance on business definitions (e.g. which revenue metric, fiscal calendar, records to exclude). Always query this first with the user's question."
  - tool_spec:
      type: cortex_analyst_text_to_sql
      name: query_sales
      description: "Query retail sales data: orders with gross, discount and returned amounts, channel, order date, stores (region, state) and products (category). Use for any revenue, units, orders question."
tool_resources:
  lessons_memory:
    search_service: HAEBI_DEMO.RLHF_MEMORY.LESSONS_SEARCH
    max_results: 4
    id_column: LESSON_ID
    title_column: EXAMPLE_QUESTION
  query_sales:
    semantic_view: HAEBI_DEMO.RLHF_MEMORY.RETAIL_SALES_SV
    execution_environment: {type: warehouse, warehouse: COMPUTE_WH, query_timeout: 299}
$$;

-- Control for the eval: identical, minus the memory tool and the "check lessons first" instruction.
CREATE OR REPLACE AGENT BASELINE_NO_MEMORY_AGENT
  COMMENT = 'Eval baseline: same as RLHF_MEMORY_AGENT but without lessons_memory tool'
  FROM SPECIFICATION $$
models:
  orchestration: auto
orchestration:
  budget: {seconds: 900, tokens: 400000}
instructions:
  response: "Answer concisely with the key number(s). State the definitions and date range used."
  orchestration: "You are a retail sales analytics assistant. Call query_sales to answer."
tools:
  - tool_spec:
      type: cortex_analyst_text_to_sql
      name: query_sales
      description: "Query retail sales data: orders with gross, discount and returned amounts, channel, order date, stores (region, state) and products (category). Use for any revenue, units, orders question."
tool_resources:
  query_sales:
    semantic_view: HAEBI_DEMO.RLHF_MEMORY.RETAIL_SALES_SV
    execution_environment: {type: warehouse, warehouse: COMPUTE_WH, query_timeout: 299}
$$;

-- ---------------------------------------------------------------- 5. Feedback joined to its trace
-- Agent feedback and the request trace share ai.observability.record_id. Reading full trace text requires
-- the READ UNREDACTED AI OBSERVABILITY EVENTS TABLE account privilege and MONITOR on the agent.

CREATE OR REPLACE VIEW FEEDBACK_WITH_TRACE AS
WITH ev AS (
  SELECT * FROM TABLE(SNOWFLAKE.LOCAL.GET_AI_OBSERVABILITY_EVENTS(
    'HAEBI_DEMO', 'RLHF_MEMORY', 'RLHF_MEMORY_AGENT', 'CORTEX AGENT'))
), fb AS (
  SELECT RECORD_ATTRIBUTES:"ai.observability.record_id"::STRING AS request_id,
         RECORD_ATTRIBUTES:"snow.ai.observability.user.name"::STRING AS feedback_user,
         VALUE:positive::BOOLEAN AS positive,
         VALUE:feedback_message::STRING AS feedback_message,
         VALUE:categories AS categories,
         TIMESTAMP AS feedback_ts
  FROM ev WHERE RECORD:name = 'CORTEX_AGENT_FEEDBACK'
), rr AS (
  SELECT RECORD_ATTRIBUTES:"ai.observability.record_id"::STRING AS request_id,
         RECORD_ATTRIBUTES:"snow.ai.observability.agent.thread_id"::STRING AS thread_id,
         RECORD_ATTRIBUTES:"ai.observability.record_root.input"::STRING AS user_question,
         RECORD_ATTRIBUTES:"ai.observability.record_root.output"::STRING AS agent_answer
  FROM ev WHERE RECORD:name = 'AgentV2RequestResponseInfo'
), sq AS (
  SELECT RECORD_ATTRIBUTES:"ai.observability.record_id"::STRING AS request_id,
         LISTAGG(RECORD_ATTRIBUTES:"snow.ai.observability.agent.tool.sql_execution.final_sql"::STRING, '\n;\n') AS generated_sql,
         LISTAGG(LEFT(RECORD_ATTRIBUTES:"snow.ai.observability.agent.tool.sql_execution.result"::STRING, 2000), '\n') AS sql_result
  FROM ev WHERE RECORD:name = 'SystemExecuteSQLTool_system_execute_sql'
  GROUP BY 1
)
SELECT fb.*, rr.thread_id, rr.user_question, rr.agent_answer, sq.generated_sql, sq.sql_result
FROM fb LEFT JOIN rr USING (request_id) LEFT JOIN sq USING (request_id);

-- Next: deploy review_app/streamlit_app.py as a Streamlit in Snowflake app to review feedback and approve lessons.
