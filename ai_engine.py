"""
ai_engine.py  —  NeuroClass AI backend

Direct port of the logic from NeuroClass_v4_Final.ipynb:
  - FallbackLLM  : Gemini-2.0-Flash → OpenRouter/Llama-3.3-70B → Groq/Llama-3.3-70B → Groq/Llama-3.1-8B
  - RAG          : FAISS + all-MiniLM-L6-v2  (build_rag_index / rag_query)
                   FIX: MMR retrieval + k=15 + smaller chunks (500/100) to handle
                        multi-PDF classrooms without answer dilution.
  - Assignment grading  : LangGraph pipeline (extract → relevance_check → evaluate → lock)
  - Project grading     : clone/pull repo → relevance check → rubric eval
  - Project advisory    : clone/pull repo → advisory report (NOT locked)

── WHY THE OLD CODE BROKE WITH MULTIPLE PDFs ───────────────────────────────
  With 1 PDF  → FAISS had ~35 chunks.  k=5 pulled 5 chunks, ALL from that PDF → great answers.
  With 5 PDFs → FAISS has ~170 chunks. k=5 still pulled 5 chunks, but they were scattered
                across PDFs by cosine similarity, so Lecture-9 content was drowned out by
                Lecture-1 syllabus text (which mentions every lecture by name).

  Three fixes applied:
    1. chunk_size 800→500, overlap 120→100  — more, smaller chunks = finer retrieval granularity
    2. k=5 → k=15  — retrieve more candidates before re-ranking
    3. MMR (Maximal Marginal Relevance) search instead of plain similarity search
       — MMR re-ranks the k=15 candidates to maximise both relevance AND diversity,
         so you get chunks from different parts of the correct lecture instead of
         5 near-duplicate syllabus sentences.
    4. Prompt rewritten: instead of "use ONLY the material" (which caused the bot to
       say "I don't have info") → now "use the material first, then general knowledge".
"""

import os
import re
import threading
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────
#  API KEYS  (read from environment / .env)
# ─────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

GEMINI_API_KEY     = os.getenv('GEMINI_API_KEY', '')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY', '')
GROQ_API_KEY       = os.getenv('GROQ_API_KEY', '')

if GEMINI_API_KEY:
    os.environ['GEMINI_API_KEY'] = GEMINI_API_KEY
if OPENROUTER_API_KEY:
    os.environ['OPENROUTER_API_KEY'] = OPENROUTER_API_KEY
if GROQ_API_KEY:
    os.environ['GROQ_API_KEY'] = GROQ_API_KEY

# ─────────────────────────────────────────────────
#  PATHS
# ─────────────────────────────────────────────────
from config import Config
LECTURES_BASE_DIR = Config.LECTURES_BASE_DIR
RAG_INDEX_DIR     = Config.RAG_INDEX_DIR


# ═══════════════════════════════════════════════════════════
#  FALLBACK LLM MANAGER
#  Priority: Gemini-2.0-Flash → OpenRouter/Llama-3.3-70B
#           → Groq/Llama-3.3-70B → Groq/Llama-3.1-8B
# ═══════════════════════════════════════════════════════════

FALLBACK_TRIGGERS = [
    'quota', 'rate', 'limit', '429', 'exceeded', 'not found',
    '404', 'no endpoints', 'unavailable', 'overloaded', '503',
    'capacity', 'decommissioned', 'deprecated', 'notfound',
    'invalid api key', 'api key not valid', 'api key invalid',
    'auth', 'permission', 'unauthorized', '400', '401', '403',
]


class FallbackLLM:
    """Auto-switches to the next provider on quota / rate-limit errors."""

    def __init__(self):
        self.models: list = []
        self.active_idx: int = 0
        self._init_models()

    def _init_models(self):
        # 1. Gemini 2.0 Flash
        if GEMINI_API_KEY:
            try:
                from langchain_google_genai import ChatGoogleGenerativeAI
                self.models.append((
                    'Gemini-2.0-Flash',
                    ChatGoogleGenerativeAI(
                        model='gemini-2.0-flash',
                        google_api_key=GEMINI_API_KEY,
                        temperature=0.2,
                    )
                ))
                print('[AI] Gemini-2.0-Flash loaded')
            except Exception as e:
                print(f'[AI] Gemini init failed: {e}')

        # 2. OpenRouter Llama-3.3-70B
        if OPENROUTER_API_KEY:
            try:
                from langchain_openai import ChatOpenAI
                self.models.append((
                    'OpenRouter/Llama-3.3-70B',
                    ChatOpenAI(
                        model='meta-llama/llama-3.3-70b-instruct:free',
                        openai_api_base='https://openrouter.ai/api/v1',
                        openai_api_key=OPENROUTER_API_KEY,
                        temperature=0.2,
                        default_headers={
                            'HTTP-Referer': 'https://neuroclass.app',
                            'X-Title': 'NeuroClass',
                        },
                    )
                ))
                print('[AI] OpenRouter/Llama-3.3-70B loaded')
            except Exception as e:
                print(f'[AI] OpenRouter init failed: {e}')

        # 3. Groq Llama-3.3-70B
        if GROQ_API_KEY:
            try:
                from langchain_groq import ChatGroq
                self.models.append((
                    'Groq/Llama-3.3-70B',
                    ChatGroq(
                        model='llama-3.3-70b-versatile',
                        api_key=GROQ_API_KEY,
                        temperature=0.2,
                    )
                ))
                print('[AI] Groq/Llama-3.3-70B loaded')
            except Exception as e:
                print(f'[AI] Groq-70B init failed: {e}')

            # 4. Groq Llama-3.1-8B (fast fallback)
            try:
                from langchain_groq import ChatGroq
                self.models.append((
                    'Groq/Llama-3.1-8B',
                    ChatGroq(
                        model='llama-3.1-8b-instant',
                        api_key=GROQ_API_KEY,
                        temperature=0.2,
                    )
                ))
                print('[AI] Groq/Llama-3.1-8B loaded')
            except Exception as e:
                print(f'[AI] Groq-8B init failed: {e}')

        if not self.models:
            print('[AI] WARNING: No LLM provider loaded. Check your API keys in .env')

    def invoke(self, messages):
        from langchain_core.messages import HumanMessage
        if not self.models:
            return type('R', (), {'content': 'No AI provider available. Please configure API keys.'})()  # noqa

        for idx in range(self.active_idx, len(self.models)):
            name, model = self.models[idx]
            try:
                result = model.invoke(messages)
                if idx != self.active_idx:
                    print(f'[AI] Switched to {name}')
                    self.active_idx = idx
                return result
            except Exception as e:
                err = str(e).lower()
                if any(t in err for t in FALLBACK_TRIGGERS):
                    print(f'[AI] {name} unavailable ({str(e)[:80]}), trying next...')
                    continue
                raise
        raise RuntimeError('All LLM providers failed.')

    @property
    def current_name(self):
        if self.models:
            return self.models[self.active_idx][0]
        return 'None'


_llm_manager: Optional[FallbackLLM] = None
_llm_lock = threading.Lock()


def get_llm() -> FallbackLLM:
    global _llm_manager
    with _llm_lock:
        if _llm_manager is None:
            _llm_manager = FallbackLLM()
    return _llm_manager


# ═══════════════════════════════════════════════════════════
#  RAG — FAISS + sentence-transformers
#
#  KEY PARAMETERS (tuned for multi-PDF classrooms):
#    CHUNK_SIZE    = 500   (was 800) — smaller chunks = more precise retrieval
#    CHUNK_OVERLAP = 100   (was 120) — slight reduction to match smaller chunks
#    RAG_K         = 15    (was 5)   — fetch 15 candidates, then MMR re-ranks to 8
#    MMR fetch_k   = 15              — pool size fed into MMR
#    MMR k         = 8               — final diverse chunks sent to LLM
# ═══════════════════════════════════════════════════════════

CHUNK_SIZE    = 500
CHUNK_OVERLAP = 100
RAG_K         = 8    # final number of chunks passed to LLM after MMR
MMR_FETCH_K   = 20   # candidate pool size for MMR (must be >= RAG_K)
MMR_LAMBDA    = 0.6  # 0=max diversity, 1=max relevance  (0.6 = good balance)

_vector_stores: dict = {}   # classroom_id (int) → FAISS store
_embedding_fn  = None
_training_status: dict = {}  # classroom_id → 'idle' | 'running' | 'done' | 'error'


def _get_embedding_fn():
    global _embedding_fn
    if _embedding_fn is None:
        from langchain_community.embeddings import HuggingFaceEmbeddings
        _embedding_fn = HuggingFaceEmbeddings(
            model_name='all-MiniLM-L6-v2',
            model_kwargs={'device': 'cpu'},
        )
    return _embedding_fn


def _safe_name(classroom_id: int) -> str:
    return str(classroom_id)


def _index_path(classroom_id: int) -> Path:
    return Path(RAG_INDEX_DIR) / _safe_name(classroom_id)


def is_indexed(classroom_id: int) -> bool:
    return classroom_id in _vector_stores or _index_path(classroom_id).exists()


def get_training_status(classroom_id: int) -> str:
    if classroom_id in _vector_stores:
        return 'done'
    if _index_path(classroom_id).exists():
        return 'done'
    return _training_status.get(classroom_id, 'idle')


def build_rag_index(classroom_id: int) -> dict:
    """
    Build the FAISS index for a classroom from its uploaded lecture files.
    Uses smaller chunks (500/100) for better per-lecture retrieval accuracy.
    Runs in a background thread so the HTTP response returns immediately.

    IMPORTANT: After adding new PDFs, call this again — it rebuilds the entire
    index so all lectures are re-embedded together in one unified vector store.
    """
    lecture_dir = Path(LECTURES_BASE_DIR) / str(classroom_id)
    if not lecture_dir.exists():
        return {'ok': False, 'error': 'No lecture files found. Upload PDFs first.'}

    pdf_files = (list(lecture_dir.glob('*.pdf')) +
                 list(lecture_dir.glob('*.txt')) +
                 list(lecture_dir.glob('*.doc')) +
                 list(lecture_dir.glob('*.docx')))

    if not pdf_files:
        return {'ok': False, 'error': 'No lecture files found. Upload PDFs first.'}

    _training_status[classroom_id] = 'running'

    def _build():
        try:
            from langchain_community.document_loaders import PyPDFDirectoryLoader
            from langchain.text_splitter import RecursiveCharacterTextSplitter
            from langchain_community.vectorstores import FAISS
            from langchain.schema import Document

            emb = _get_embedding_fn()

            # ── Load PDFs ──
            loader = PyPDFDirectoryLoader(str(lecture_dir), glob='**/*.pdf', silent_errors=True)
            docs   = loader.load()

            # ── Tag each doc with its source filename for debugging ──
            for doc in docs:
                src = doc.metadata.get('source', '')
                doc.metadata['filename'] = Path(src).name if src else 'unknown'

            # ── Load .txt files ──
            for txt_file in lecture_dir.glob('*.txt'):
                try:
                    text = txt_file.read_text(encoding='utf-8', errors='replace')
                    docs.append(Document(
                        page_content=text,
                        metadata={'source': str(txt_file), 'filename': txt_file.name}
                    ))
                except Exception:
                    pass

            if not docs:
                _training_status[classroom_id] = 'error'
                print(f'[RAG] No text extracted from files in {lecture_dir}')
                return

            # ── Chunk with SMALLER size for finer retrieval granularity ──
            chunks = RecursiveCharacterTextSplitter(
                chunk_size=CHUNK_SIZE,
                chunk_overlap=CHUNK_OVERLAP,
                separators=['\n\n', '\n', '. ', ' ', ''],
            ).split_documents(docs)

            store = FAISS.from_documents(chunks, emb)
            _vector_stores[classroom_id] = store

            idx_path = _index_path(classroom_id)
            idx_path.mkdir(parents=True, exist_ok=True)
            store.save_local(str(idx_path))

            _training_status[classroom_id] = 'done'
            print(
                f'[RAG] Index built for classroom {classroom_id}: '
                f'{len(docs)} pages, {len(chunks)} chunks → {idx_path}'
            )
        except Exception as e:
            _training_status[classroom_id] = 'error'
            print(f'[RAG] Build failed for classroom {classroom_id}: {e}')

    t = threading.Thread(target=_build, daemon=True)
    t.start()
    return {'ok': True, 'message': f'Training started for {len(pdf_files)} file(s). Refresh in ~30 seconds.'}


def _load_rag_index(classroom_id: int) -> bool:
    """Load a saved FAISS index from disk."""
    idx_path = _index_path(classroom_id)
    if not idx_path.exists():
        return False
    try:
        from langchain_community.vectorstores import FAISS
        emb = _get_embedding_fn()
        _vector_stores[classroom_id] = FAISS.load_local(
            str(idx_path), emb, allow_dangerous_deserialization=True
        )
        print(f'[RAG] Loaded index for classroom {classroom_id}')
        return True
    except Exception as e:
        print(f'[RAG] Load failed for classroom {classroom_id}: {e}')
        return False


# Per-user conversation memory  {session_key: [HumanMessage / AIMessage]}
_conversation_memory: dict = {}


def rag_query(
    classroom_id: int,
    question: str,
    k: int = RAG_K,
    context_data: Optional[dict] = None,
    session_key: Optional[str] = None,
) -> str:
    """
    Answer a student's question using MMR retrieval over the classroom's FAISS
    index, then the active LLM.  Conversation memory is maintained per session.

    Retrieval strategy (multi-PDF safe):
      1. FAISS MMR search with fetch_k=MMR_FETCH_K candidates
      2. MMR re-ranks to k=RAG_K diverse chunks (lambda=MMR_LAMBDA)
      3. All chunks concatenated → injected into LLM prompt
      4. LLM instructed to USE the material as primary source, then supplement
         with general knowledge — this avoids the "I don't have info" dead-end.
    """
    from langchain_core.messages import HumanMessage, AIMessage
    llm = get_llm()

    # ── Build assignment context string ──
    assign_ctx = ''
    if context_data and context_data.get('assignments'):
        lines = ['Upcoming assignments:']
        for a in context_data['assignments']:
            lines.append(f"  - {a['title']} | Due: {a['due_date']} | Max marks: {a['max_marks']}")
        assign_ctx = '\n'.join(lines)

    class_name   = (context_data or {}).get('class_name', '')
    subject      = (context_data or {}).get('subject', '')
    student_name = (context_data or {}).get('student_name', '')

    # ── Load index if not in memory ──
    if classroom_id not in _vector_stores:
        _load_rag_index(classroom_id)

    # ── MMR retrieval ──────────────────────────────────────────────────────
    #  MMR (Maximal Marginal Relevance) solves the "all chunks are the same"
    #  problem by penalising redundant results.  It fetches MMR_FETCH_K
    #  candidates by cosine similarity, then greedily picks k documents that
    #  are both relevant AND diverse from each other.
    # ──────────────────────────────────────────────────────────────────────
    context_chunks = ''
    source_files   = []

    if classroom_id in _vector_stores:
        try:
            store = _vector_stores[classroom_id]
            total_chunks = store.index.ntotal  # total vectors in FAISS index

            # Clamp fetch_k to what's actually in the index
            actual_fetch_k = min(MMR_FETCH_K, total_chunks)
            actual_k       = min(k, actual_fetch_k)

            docs = store.max_marginal_relevance_search(
                question,
                k=actual_k,
                fetch_k=actual_fetch_k,
                lambda_mult=MMR_LAMBDA,
            )

            context_chunks = '\n\n---\n\n'.join(d.page_content for d in docs)

            # Collect unique source filenames for transparency
            source_files = list(dict.fromkeys(
                d.metadata.get('filename', d.metadata.get('source', ''))
                for d in docs
            ))

            print(
                f'[RAG] MMR query for classroom={classroom_id}: '
                f'fetched {actual_fetch_k} → kept {actual_k} chunks '
                f'from {source_files}'
            )
        except Exception as e:
            print(f'[RAG] MMR retrieval error: {e}')
            # Fallback to plain similarity search if MMR fails
            try:
                retriever  = _vector_stores[classroom_id].as_retriever(
                    search_kwargs={'k': min(k, total_chunks)}
                )
                docs           = retriever.invoke(question)
                context_chunks = '\n\n---\n\n'.join(d.page_content for d in docs)
                print('[RAG] Fell back to plain similarity search')
            except Exception as e2:
                print(f'[RAG] Fallback retrieval also failed: {e2}')

    # ── Conversation memory ──
    history_msgs = []
    if session_key:
        history_msgs = _conversation_memory.get(session_key, [])[-10:]  # last 5 turns

    # ── Build prompt ──────────────────────────────────────────────────────
    #  FIX: old prompt said "use ONLY the material" which caused the LLM to
    #  refuse answering when the retrieved chunks happened to be from a
    #  different lecture.  New prompt:
    #    - Prioritise the retrieved course material
    #    - If the answer is partially or fully absent, fill in with general
    #      knowledge and clearly label it
    #    - Never say "I don't have information" when you can still help
    # ──────────────────────────────────────────────────────────────────────
    if context_chunks:
        sources_str = ', '.join(source_files) if source_files else 'course materials'
        system_prompt = (
            f"You are a knowledgeable and helpful AI teaching assistant for the "
            f"'{class_name}' ({subject}) classroom on NeuroClass.\n"
            f"Student's name: {student_name}\n\n"
            f"{assign_ctx}\n\n"
            "INSTRUCTIONS:\n"
            "1. The COURSE MATERIAL below contains excerpts retrieved from the uploaded "
            f"lecture notes ({sources_str}).\n"
            "2. Use this material as your PRIMARY source of truth. Quote or paraphrase it "
            "when answering.\n"
            "3. If the material covers the topic partially, complete your answer using your "
            "general knowledge and clearly say: '[General knowledge]:' before that part.\n"
            "4. If the material does NOT cover the topic at all, answer from general knowledge "
            "and say: '[Note: Answer based on general knowledge — not in the lecture notes]'\n"
            "5. NEVER say 'I don't have information about this'. Always try to help.\n"
            "6. Be concise, clear, and student-friendly.\n\n"
            f"COURSE MATERIAL (from {sources_str}):\n"
            "─────────────────────────────────────\n"
            f"{context_chunks}\n"
            "─────────────────────────────────────"
        )
    else:
        system_prompt = (
            f"You are a helpful AI teaching assistant for the '{class_name}' ({subject}) "
            f"classroom on NeuroClass.\n"
            f"Student's name: {student_name}\n\n"
            f"{assign_ctx}\n\n"
            "No lecture notes have been indexed for this classroom yet. "
            "Answer based on your general knowledge and be as helpful as possible. "
            "Mention that the teacher should upload lecture PDFs for more accurate answers."
        )

    messages = (
        [HumanMessage(content=system_prompt)]
        + history_msgs
        + [HumanMessage(content=question)]
    )

    answer_obj = llm.invoke(messages)
    answer     = answer_obj.content if hasattr(answer_obj, 'content') else str(answer_obj)

    # ── Update conversation memory ──
    if session_key:
        mem = _conversation_memory.get(session_key, [])
        mem.append(HumanMessage(content=question))
        mem.append(AIMessage(content=answer))
        _conversation_memory[session_key] = mem[-20:]  # keep last 10 turns

    return answer


# ═══════════════════════════════════════════════════════════
#  ASSIGNMENT GRADER  (LangGraph pipeline from the notebook)
# ═══════════════════════════════════════════════════════════

def _build_assignment_graph():
    from typing import TypedDict
    from langgraph.graph import StateGraph, END
    from langchain_community.document_loaders import PyPDFLoader
    from langchain_core.messages import HumanMessage

    class EvalState(TypedDict):
        submission_path: str
        rubric: str
        course_id: str
        student_id: str
        extracted_text: str
        relevance_flags: str
        evaluation: str
        score: float
        feedback: str
        locked: bool

    def node_extract(state: EvalState) -> EvalState:
        p = Path(state['submission_path'])
        if p.exists():
            pages = PyPDFLoader(str(p)).load()
            state['extracted_text'] = ' '.join(pg.page_content for pg in pages)
            print(f'[Grader] Extracted {len(pages)} pages')
        else:
            state['extracted_text'] = 'ERROR: File not found'
        return state

    def node_relevance_check(state: EvalState) -> EvalState:
        if 'ERROR' in state['extracted_text']:
            state['relevance_flags'] = 'ON_TOPIC: NO | HAS_DOCUMENTATION: NO'
            return state
        llm = get_llm()
        prompt = (
            f"You are checking academic submission relevance for NeuroClass.\n\n"
            f"ASSIGNMENT RUBRIC / EXPECTED TOPIC:\n{state['rubric']}\n\n"
            f"SUBMISSION CONTENT (first 2000 chars):\n{state['extracted_text'][:2000]}\n\n"
            "Answer ONLY these two questions, strictly YES or NO.\n"
            "- ON_TOPIC: Does the submission directly address the assignment rubric?\n"
            "- HAS_DOCUMENTATION: Does it contain comments, docstrings, or a README?\n\n"
            "Respond in EXACTLY this format:\n"
            "ON_TOPIC: YES/NO — one sentence reason\n"
            "HAS_DOCUMENTATION: YES/NO — one sentence reason"
        )
        resp = llm.invoke([HumanMessage(content=prompt)])
        state['relevance_flags'] = resp.content.strip()
        return state

    def node_evaluate(state: EvalState) -> EvalState:
        if 'ERROR' in state['extracted_text']:
            state.update(evaluation='ERROR', score=0.0, feedback='Could not read file. Score: 0.', locked=True)
            return state
        llm = get_llm()
        flags = state.get('relevance_flags', '')
        off_topic_warn = (
            'RELEVANCE WARNING: Submission flagged as OFF-TOPIC. '
            'Max score is 30.\n' if 'ON_TOPIC: NO' in flags.upper() else ''
        )
        no_docs_warn = (
            'DOCUMENTATION WARNING: No documentation detected. '
            'Documentation criteria score = 0.\n' if 'HAS_DOCUMENTATION: NO' in flags.upper() else ''
        )
        prompt = (
            f"{off_topic_warn}{no_docs_warn}"
            f"ASSIGNMENT RUBRIC:\n{state['rubric']}\n\n"
            f"STUDENT SUBMISSION:\n{state['extracted_text']}\n\n"
            f"PRE-EVALUATION FLAGS:\n{flags}\n\n"
            "Respond in EXACTLY this format:\n"
            "CRITERION_BREAKDOWN:\n"
            "- criterion: score/max — reason\n"
            "SCORE: 0-100\n"
            "GRADE: A/B/C/D/F\n"
            "STRENGTHS: ...\n"
            "WEAKNESSES: ...\n"
            "IMPROVEMENT_SUGGESTIONS:\n- ...\n"
            "DETAILED_FEEDBACK: ..."
        )
        raw = llm.invoke([HumanMessage(content=prompt)]).content
        score = 0.0
        for line in raw.split('\n'):
            if line.strip().upper().startswith('SCORE:'):
                try:
                    score = float(line.split(':', 1)[1].strip().split()[0])
                except (ValueError, IndexError):
                    pass
                break
        if 'ON_TOPIC: NO' in flags.upper() and score > 30:
            score = min(score, 30.0)
        state.update(evaluation=raw, score=score, feedback=raw, locked=True)
        return state

    def node_lock(state: EvalState) -> EvalState:
        state['locked'] = True
        grade = 'A' if state['score'] >= 90 else 'B' if state['score'] >= 80 else \
                'C' if state['score'] >= 70 else 'D' if state['score'] >= 60 else 'F'
        print(f"[Grader] LOCKED student={state['student_id']} score={state['score']} grade={grade}")
        return state

    g = StateGraph(EvalState)
    g.add_node('extract',         node_extract)
    g.add_node('relevance_check', node_relevance_check)
    g.add_node('evaluate',        node_evaluate)
    g.add_node('lock',            node_lock)
    g.set_entry_point('extract')
    g.add_edge('extract',         'relevance_check')
    g.add_edge('relevance_check', 'evaluate')
    g.add_edge('evaluate',        'lock')
    g.add_edge('lock',            END)
    return g.compile()


_assignment_chain = None
_assignment_chain_lock = threading.Lock()


def get_assignment_chain():
    global _assignment_chain
    with _assignment_chain_lock:
        if _assignment_chain is None:
            _assignment_chain = _build_assignment_graph()
    return _assignment_chain


def evaluate_assignment(submission_pdf: str, rubric: str, course_id: str, student_id: str) -> dict:
    """
    Grade a student's PDF submission against a rubric.
    Returns dict with keys: score (float), feedback (str), locked (bool).
    """
    chain = get_assignment_chain()
    result = chain.invoke({
        'submission_path': submission_pdf,
        'rubric':          rubric,
        'course_id':       course_id,
        'student_id':      student_id,
        'extracted_text':  '',
        'relevance_flags': '',
        'evaluation':      '',
        'score':           0.0,
        'feedback':        '',
        'locked':          False,
    })
    return result


# ═══════════════════════════════════════════════════════════
#  PROJECT GRADER  (from notebook cells 5 / 5b)
# ═══════════════════════════════════════════════════════════

def _clone_or_pull(repo_url: str, local_path: str) -> bool:
    import subprocess
    p = Path(local_path)
    cmd = ['git', '-C', local_path, 'pull'] if p.exists() else ['git', 'clone', repo_url, local_path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return result.returncode == 0


def _get_repo_summary(local_path: str) -> str:
    import subprocess
    parts = []
    r = subprocess.run(
        ['find', local_path, '-type', 'f',
         '-not', '-path', '*/.git/*',
         '-not', '-path', '*/node_modules/*',
         '-not', '-path', '*/__pycache__/*'],
        capture_output=True, text=True
    )
    files = [f.replace(local_path, '').lstrip('/') for f in r.stdout.strip().split('\n') if f][:60]
    parts.append('FILE STRUCTURE (sample):\n' + '\n'.join(files))
    r = subprocess.run(
        ['git', '-C', local_path, 'log', '--oneline', '--format=%h %ad %s', '--date=short', '-20'],
        capture_output=True, text=True
    )
    parts.append('COMMITS:\n' + r.stdout)
    for name in ['README.md', 'readme.md', 'README.txt']:
        readme = Path(local_path) / name
        if readme.exists():
            parts.append('README:\n' + readme.read_text(errors='ignore')[:2000])
            break
    return '\n\n'.join(parts)


def _check_repo_relevance(summary: str, project_details: str, project_rubric: str) -> dict:
    from langchain_core.messages import HumanMessage
    llm = get_llm()
    detail_block = project_details or project_rubric
    prompt = (
        f"You are a strict academic integrity checker for NeuroClass.\n\n"
        f"PROJECT REQUIREMENTS:\n{detail_block}\n\n"
        f"STUDENT REPO ANALYSIS:\n{summary}\n\n"
        "Answer these two questions with strict YES or NO:\n"
        "Q1_HAS_CODE: Does the repository contain actual code files? YES or NO\n"
        "Q2_IS_RELEVANT: Is the code DIRECTLY related to the project requirements? YES or NO\n"
        "REASON: One sentence explaining your decision."
    )
    response = llm.invoke([HumanMessage(content=prompt)]).content
    lines = {l.split(':')[0].strip().upper(): l.split(':', 1)[1].strip()
             for l in response.splitlines() if ':' in l}
    has_code    = 'YES' in lines.get('Q1_HAS_CODE',   '').upper()
    is_relevant = 'YES' in lines.get('Q2_IS_RELEVANT', '').upper()
    reason      = lines.get('REASON', response[:200])
    return {'relevant': has_code and is_relevant, 'has_code': has_code, 'is_relevant': is_relevant, 'reason': reason}


def evaluate_project(
    repo_url: str,
    project_rubric: str,
    project_details: str,
    student_id: str,
    classroom_id: int,
) -> dict:
    """
    Strict project evaluation: relevance gate → rubric grading → lock.
    """
    from langchain_core.messages import HumanMessage
    local_path = str(Path(Config.UPLOAD_FOLDER) / 'student_repos' / str(classroom_id) / str(student_id))
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)

    if not _clone_or_pull(repo_url, local_path):
        return {'error': f'Could not clone {repo_url}. Ensure the repo is public.'}

    summary = _get_repo_summary(local_path)
    rel     = _check_repo_relevance(summary, project_details, project_rubric)

    if not rel['relevant']:
        reason_type = 'EMPTY_OR_NO_CODE' if not rel['has_code'] else 'WRONG_PROJECT'
        msg = 'No meaningful code found.' if not rel['has_code'] else 'Repo not related to the project.'
        feedback = (
            f"AUTOMATIC REJECTION: {reason_type}\n"
            f"SCORE: 0 | GRADE: F\n"
            f"REASON: {msg}\n"
            f"AI CHECK: {rel['reason']}\n"
            f"Repo submitted: {repo_url}"
        )
        return {'student_id': student_id, 'classroom_id': classroom_id,
                'repo_url': repo_url, 'analysis': feedback,
                'score': 0.0, 'grade': 'F', 'locked': True, 'rejected': True}

    llm = get_llm()
    detail_block = f'PROJECT REQUIREMENTS:\n{project_details}\n\n' if project_details else ''
    prompt = (
        f"{detail_block}RUBRIC:\n{project_rubric}\n\n"
        f"REPO ANALYSIS:\n{summary}\n\n"
        "Evaluate the student project strictly against the rubric.\n"
        "Respond in EXACTLY this format:\n"
        "CRITERION_BREAKDOWN:\n- criterion: pts_earned/pts_total — reason\n"
        "SCORE: 0-100\nGRADE: A/B/C/D/F\n"
        "STRENGTHS: ...\nWEAKNESSES: ...\n"
        "IMPROVEMENT_SUGGESTIONS:\n- ...\nDETAILED_FEEDBACK: ..."
    )
    analysis = llm.invoke([HumanMessage(content=prompt)]).content
    score = 0.0
    for line in analysis.splitlines():
        if line.strip().upper().startswith('SCORE:'):
            try:
                score = float(line.split(':', 1)[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
            break
    grade = 'A' if score >= 90 else 'B' if score >= 80 else 'C' if score >= 70 else 'D' if score >= 60 else 'F'
    print(f'[Project] LOCKED student={student_id} score={score} grade={grade}')
    return {'student_id': student_id, 'classroom_id': classroom_id,
            'repo_url': repo_url, 'analysis': analysis,
            'score': score, 'grade': grade, 'locked': True, 'rejected': False}


def analyze_project_advisory(
    repo_url: str,
    project_rubric: str,
    student_id: str,
    classroom_id: int,
    project_details: str = '',
) -> dict:
    """
    Advisory (non-locking) project analysis — gives next steps.
    """
    from langchain_core.messages import HumanMessage
    local_path = str(Path(Config.UPLOAD_FOLDER) / 'student_repos' / str(classroom_id) / f'{student_id}_advisory')
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)

    if not _clone_or_pull(repo_url, local_path):
        return {'error': f'Could not clone {repo_url}.'}

    summary      = _get_repo_summary(local_path)
    detail_block = f'PROJECT REQUIREMENTS:\n{project_details}\n\n' if project_details else ''
    llm = get_llm()
    prompt = (
        f"{detail_block}RUBRIC:\n{project_rubric}\n\n"
        f"REPO ANALYSIS:\n{summary}\n\n"
        "Respond in EXACTLY this format:\n"
        "COMPLETION_PERCENTAGE: 0-100\n"
        "CURRENT_STATUS: ...\n"
        "WHAT_IS_DONE_WELL:\n- ...\n"
        "WHAT_IS_MISSING:\n- ...\n"
        "NEXT_STEPS:\n1. ...\n"
        "ESTIMATED_GRADE_IF_SUBMITTED_NOW: A/B/C/D/F — reason"
    )
    analysis = llm.invoke([HumanMessage(content=prompt)]).content
    return {'student_id': student_id, 'classroom_id': classroom_id,
            'repo_url': repo_url, 'analysis': analysis}
