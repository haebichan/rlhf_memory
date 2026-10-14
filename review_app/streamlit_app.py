"""RLHF Memory Review: Streamlit in Snowflake app for the human-approval step.

  1. Inbox   - pull new thumbs-down feedback into REVIEW_QUEUE and draft case files with AI_COMPLETE
  2. Review  - a domain owner edits the proposed lesson, then approves or rejects it
  3. Memory  - see approved lessons and retire stale ones

Approved rows in LESSONS are picked up by LESSONS_SEARCH (status = 'APPROVED', TARGET_LAG = 1 minute),
so an approved lesson reaches the agent within about a minute. Retired lessons drop out the same way.

The app owner role needs: SELECT/INSERT/UPDATE on REVIEW_QUEUE and LESSONS, SELECT on FEEDBACK_WITH_TRACE,
MONITOR on RLHF_MEMORY_AGENT, the READ UNREDACTED AI OBSERVABILITY EVENTS TABLE privilege, and
SNOWFLAKE.CORTEX_USER for AI_COMPLETE.
"""
import json

import streamlit as st

SCHEMA = "HAEBI_DEMO.RLHF_MEMORY"
MODEL = "claude-sonnet-4-5"  # any AI_COMPLETE model available in your region
TIERS = {
    "TIER2_MEMORY": "Tier 2: lesson memory (retrieved when relevant)",
    "TIER1_SOURCE_OF_TRUTH": "Tier 1: source of truth (also fix the semantic view / instructions)",
}

st.set_page_config(page_title="RLHF Memory Review", layout="wide")


@st.cache_resource
def get_session():
    try:
        from snowflake.snowpark.context import get_active_session

        return get_active_session()  # Streamlit in Snowflake
    except Exception:
        return st.connection("snowflake").session()  # local `streamlit run`


session = get_session()


def query(sql, params=None):
    return session.sql(sql, params=params or []).to_pandas()


def execute(sql, params=None):
    return session.sql(sql, params=params or []).collect()


# ---------------------------------------------------------------- capture + diagnose

CAPTURE_SQL = f"""
INSERT INTO {SCHEMA}.REVIEW_QUEUE
  (case_id, request_id, thread_id, feedback_user, feedback_positive, feedback_message,
   user_question, agent_answer, generated_sql, case_file, status)
SELECT (SELECT COALESCE(MAX(case_id), 0) FROM {SCHEMA}.REVIEW_QUEUE)
         + ROW_NUMBER() OVER (ORDER BY feedback_ts),
       request_id, thread_id, feedback_user, positive, feedback_message,
       user_question, agent_answer, generated_sql, NULL, 'PENDING'
FROM (
  SELECT * FROM {SCHEMA}.FEEDBACK_WITH_TRACE
  WHERE positive = FALSE AND request_id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY request_id ORDER BY feedback_ts DESC) = 1
) f
WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.REVIEW_QUEUE q WHERE q.request_id = f.request_id)
"""

CASE_FILE_SCHEMA = """{'type': 'json', 'schema': {'type': 'object', 'properties': {
  'what_happened': {'type': 'string'}, 'likely_issue': {'type': 'string'},
  'proposed_guidance': {'type': 'string'}, 'scope': {'type': 'string'},
  'suggested_tier': {'type': 'string', 'enum': ['TIER1_SOURCE_OF_TRUTH', 'TIER2_MEMORY']},
  'confidence': {'type': 'string', 'enum': ['high', 'medium', 'low']},
  'open_questions_for_owner': {'type': 'array', 'items': {'type': 'string'}}},
  'required': ['what_happened', 'likely_issue', 'proposed_guidance', 'scope',
               'suggested_tier', 'confidence', 'open_questions_for_owner']}}"""

DRAFT_SQL = f"""
UPDATE {SCHEMA}.REVIEW_QUEUE
SET case_file = TRY_PARSE_JSON(AI_COMPLETE(
  model => '{MODEL}',
  prompt => 'You review thumbs-down feedback on a Cortex Agent that answers retail sales questions. '
    || 'Diagnose what went wrong and draft a lesson for a human domain owner to approve. '
    || 'Never invent business rules that the feedback and data do not support; list them as open questions instead.\\n\\n'
    || 'USER QUESTION: ' || COALESCE(user_question, '') || '\\n'
    || 'AGENT ANSWER: ' || COALESCE(agent_answer, '') || '\\n'
    || 'GENERATED SQL: ' || COALESCE(generated_sql, '') || '\\n'
    || 'USER FEEDBACK: ' || COALESCE(feedback_message, ''),
  response_format => {CASE_FILE_SCHEMA}
)::STRING)
WHERE status = 'PENDING' AND case_file IS NULL
"""


def pull_new_feedback():
    inserted = execute(CAPTURE_SQL)[0][0]
    # AI_COMPLETE structured output can return NULL intermittently, so retry the rows still missing a draft.
    for _ in range(3):
        execute(DRAFT_SQL)
        if query(f"SELECT COUNT(*) N FROM {SCHEMA}.REVIEW_QUEUE "
                 "WHERE status = 'PENDING' AND case_file IS NULL").N[0] == 0:
            break
    return inserted


def parse_case(raw):
    if raw is None:
        return {}
    return json.loads(raw) if isinstance(raw, str) else raw


# ---------------------------------------------------------------- review actions

def approve(case, issue, guidance, scope, example_question, tier, reviewer):
    execute(
        f"""INSERT INTO {SCHEMA}.LESSONS
              (lesson_id, source_case_id, example_question, issue, guidance, scope, tier, status, approved_by)
            SELECT COALESCE(MAX(lesson_id), 0) + 1, ?, ?, ?, ?, ?, ?, 'APPROVED', ?
            FROM {SCHEMA}.LESSONS""",
        [int(case.CASE_ID), example_question, issue, guidance, scope, tier, reviewer],
    )
    set_status(case.CASE_ID, "APPROVED", reviewer)


def set_status(case_id, status, reviewer):
    execute(
        f"UPDATE {SCHEMA}.REVIEW_QUEUE SET status = ?, reviewer = ?, reviewed_at = CURRENT_TIMESTAMP() "
        "WHERE case_id = ?",
        [status, reviewer, int(case_id)],
    )


def default_reviewer():
    try:
        return st.experimental_user.get("email") or st.experimental_user.get("user_name") or ""
    except Exception:
        return ""


# ---------------------------------------------------------------- UI

st.title("RLHF Memory Review")
st.caption("Turn thumbs-down feedback into human-approved lessons that the Cortex Agent retrieves at runtime.")

counts = query(f"SELECT status, COUNT(*) N FROM {SCHEMA}.REVIEW_QUEUE GROUP BY 1").set_index("STATUS").N
live = query(f"SELECT COUNT(*) N FROM {SCHEMA}.LESSONS WHERE status = 'APPROVED'").N[0]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Pending review", int(counts.get("PENDING", 0)))
c2.metric("Approved", int(counts.get("APPROVED", 0)))
c3.metric("Rejected", int(counts.get("REJECTED", 0)))
c4.metric("Live lessons", int(live))

inbox, review, memory = st.tabs(["Inbox", "Review", "Memory"])

with inbox:
    st.write("Pull thumbs-down feedback that isn't in the queue yet, and draft a case file for each with AI_COMPLETE.")
    if st.button("Pull new feedback", type="primary"):
        with st.spinner("Capturing feedback and drafting case files..."):
            n = pull_new_feedback()
        st.success(f"Added {n} new case(s) to the queue." if n else "No new thumbs-down feedback.")
    st.dataframe(
        query(f"""SELECT case_id, status, feedback_message, user_question, reviewer, created_at
                  FROM {SCHEMA}.REVIEW_QUEUE ORDER BY case_id DESC"""),
        use_container_width=True, hide_index=True,
    )

with review:
    pending = query(f"SELECT * FROM {SCHEMA}.REVIEW_QUEUE WHERE status = 'PENDING' ORDER BY case_id")
    if pending.empty:
        st.info("Nothing to review. Pull new feedback from the Inbox tab.")
    else:
        labels = {f"#{r.CASE_ID}: {r.FEEDBACK_MESSAGE}": i for i, r in pending.iterrows()}
        case = pending.loc[labels[st.selectbox("Case", list(labels))]]
        cf = parse_case(case.CASE_FILE)

        left, right = st.columns(2)
        with left:
            st.subheader("What the user saw")
            st.markdown(f"**Question:** {case.USER_QUESTION}")
            st.markdown(f"**Feedback:** _{case.FEEDBACK_MESSAGE}_")
            with st.expander("Agent answer", expanded=True):
                st.markdown(case.AGENT_ANSWER or "_not captured_")
            with st.expander("Generated SQL"):
                st.code(case.GENERATED_SQL or "-- not captured", language="sql")
        with right:
            st.subheader("AI_COMPLETE diagnosis")
            if not cf:
                st.warning("No case file drafted yet. Use 'Pull new feedback' to retry.")
            else:
                st.markdown(f"**Confidence:** {cf.get('confidence', '?')}")
                st.markdown(f"**What happened:** {cf.get('what_happened', '')}")
                for q in cf.get("open_questions_for_owner", []):
                    st.markdown(f"- :orange[Open question:] {q}")

        st.divider()
        st.subheader("Lesson to approve")
        st.caption("Edit the draft. Answer the open questions in the guidance. Only the guidance text reaches the agent.")
        with st.form(f"lesson_{case.CASE_ID}"):
            guidance = st.text_area("Guidance (what the agent should do)", cf.get("proposed_guidance", ""), height=140)
            issue = st.text_area("Issue (why the answer was wrong)", cf.get("likely_issue", ""), height=100)
            col_a, col_b = st.columns(2)
            scope = col_a.text_input("Scope", cf.get("scope", ""))
            example_question = col_b.text_input("Example question", case.USER_QUESTION or "")
            suggested = cf.get("suggested_tier", "TIER2_MEMORY")
            tier = st.radio("Tier", list(TIERS), format_func=TIERS.get,
                            index=list(TIERS).index(suggested) if suggested in TIERS else 0, horizontal=True)
            reviewer = st.text_input("Reviewer", default_reviewer())
            ok, no = st.columns(2)
            approved = ok.form_submit_button("Approve lesson", type="primary", use_container_width=True)
            rejected = no.form_submit_button("Reject", use_container_width=True)

        if approved or rejected:
            if not reviewer.strip():
                st.error("Enter a reviewer name.")
            elif approved and not guidance.strip():
                st.error("Guidance can't be empty.")
            elif approved:
                approve(case, issue, guidance, scope, example_question, tier, reviewer.strip())
                st.success("Approved. The agent will retrieve this lesson within about a minute.")
                st.rerun()
            else:
                set_status(case.CASE_ID, "REJECTED", reviewer.strip())
                st.rerun()

with memory:
    lessons = query(f"""SELECT lesson_id, status, scope, guidance, tier, approved_by, updated_at
                        FROM {SCHEMA}.LESSONS ORDER BY lesson_id""")
    st.dataframe(lessons, use_container_width=True, hide_index=True)
    active = lessons[lessons.STATUS == "APPROVED"]
    if not active.empty:
        with st.form("retire"):
            lesson_id = st.selectbox("Retire a lesson", active.LESSON_ID.tolist(),
                                     format_func=lambda i: f"#{i}: {active.set_index('LESSON_ID').SCOPE[i]}")
            if st.form_submit_button("Retire"):
                execute(f"UPDATE {SCHEMA}.LESSONS SET status = 'RETIRED', updated_at = CURRENT_TIMESTAMP() "
                        "WHERE lesson_id = ?", [int(lesson_id)])
                st.rerun()
