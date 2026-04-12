"""
ai_engine.py  —  NeuroClass AI backend

Direct port of the logic from NeuroClass_v4_Final.ipynb:
  - FallbackLLM  : Gemini-2.0-Flash → OpenRouter/Llama-3.3-70B → Groq/Llama-3.3-70B → Groq/Llama-3.1-8B
  - RAG          : FAISS + all-MiniLM-L6-v2  (build_rag_index / rag_query)
  - Assignment grading  : LangGraph pipeline (extract → relevance_check → evaluate → lock)
  - Project grading     : clone/pull repo → relevance check → rubric eval
  - Project advisory    : clone/pull repo → advisory report (NOT locked)

FIXES APPLIED:
  1. Import fix: 'from langchain.schema import Document' triggers broken
     langchain_core.memory import on newer langchain versions.
     Fixed by importing Document directly from langchain_core.documents.

  2. PDF extraction: 3-layer waterfall (PyPDFLoader → pdfplumber → pypdf)
     so slide-style PDFs (IIT lecture slides) are properly read.

  3. Stale index: old FAISS folder is deleted before every retrain.

  4. faiss-cpu version pinned to >=1.9.0 in requirements.txt
"""

import os
import re
import shutil
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
#  PDF EXTRACTION — 3-layer waterfall
#
#  Layer 1: PyPDFLoader  (langchain wrapper around pypdf)
#           Fast.  Works well for text-heavy PDFs (reports, papers).
#           FAILS silently for slide PDFs where text is drawn as vectors.
#
#  Layer 2: pdfplumber
#           Better at tables and slide-style PDFs.  Uses pdfminer under
#           the hood and can parse text from many slide formats that
#           PyPDF misses.  Requires: pip install pdfplumber
#
#  Layer 3: pypdf direct
#           Last resort; sometimes catches residual text that pdfplumber
#           misses in hybrid PDFs.
#
#  If ALL three layers return < 50 chars total, the file is logged as
#  "image-only" (scanned PDF with no text layer) and skipped.
# ═══════════════════════════════════════════════════════════

def _extract_pdf_text(pdf_path: str) -> str:
    """
    Extract all text from a PDF using a 3-layer waterfall.
    Returns the combined text string, or '' if no text found.
    """
    path = Path(pdf_path)
    combined = ''

    # ── Layer 1: PyPDFLoader ──────────────────────────────────────────
    try:
        from langchain_community.document_loaders import PyPDFLoader
        pages = PyPDFLoader(str(path)).load()
        text1 = ' '.join(p.page_content for p in pages).strip()
        if text1:
            combined += text1
            print(f'[PDF-L1] {path.name}: {len(text1)} chars via PyPDFLoader')
    except Exception as e:
        print(f'[PDF-L1] {path.name} failed: {e}')

    # ── Layer 2: pdfplumber (best for slides / tables) ────────────────
    if len(combined) < 200:          # only if Layer 1 got very little
        try:
            import pdfplumber
            pages_text = []
            with pdfplumber.open(str(path)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        pages_text.append(t)
            text2 = '\n'.join(pages_text).strip()
            if text2:
                combined = (combined + '\n' + text2).strip() if combined else text2
                print(f'[PDF-L2] {path.name}: +{len(text2)} chars via pdfplumber')
        except ImportError:
            print('[PDF-L2] pdfplumber not installed — run: pip install pdfplumber')
        except Exception as e:
            print(f'[PDF-L2] {path.name} failed: {e}')

    # ── Layer 3: pypdf direct ─────────────────────────────────────────
    if len(combined) < 200:
        try:
            import pypdf
            reader = pypdf.PdfReader(str(path))
            parts  = []
            for page in reader.pages:
                t = page.extract_text() or ''
                if t.strip():
                    parts.append(t)
            text3 = '\n'.join(parts).strip()
            if text3:
                combined = (combined + '\n' + text3).strip() if combined else text3
                print(f'[PDF-L3] {path.name}: +{len(text3)} chars via pypdf direct')
        except Exception as e:
            print(f'[PDF-L3] {path.name} failed: {e}')

    if len(combined) < 50:
        print(f'[PDF-WARN] {path.name}: only {len(combined)} chars extracted — '
              'may be a scanned/image-only PDF with no text layer.')

    return combined


# ═══════════════════════════════════════════════════════════
#  RAG — FAISS + sentence-transformers
#
#  KEY PARAMETERS (tuned for multi-PDF classrooms):
#    CHUNK_SIZE    = 500   — smaller chunks = more precise retrieval
#    CHUNK_OVERLAP = 100
#    RAG_K         = 8     — final chunks sent to LLM after MMR
#    MMR_FETCH_K   = 20    — candidate pool fed into MMR
#    MMR_LAMBDA    = 0.6   — 0=max diversity, 1=max relevance
# ═══════════════════════════════════════════════════════════

CHUNK_SIZE    = 500
CHUNK_OVERLAP = 100
RAG_K         = 8
MMR_FETCH_K   = 20
MMR_LAMBDA    = 0.6

_vector_stores: dict = {}
_embedding_fn  = None
_training_status: dict = {}


def _get_embedding_fn():
    global _embedding_fn
    if _embedding_fn is None:
        try:
            # Prefer the newer non-deprecated package
            from langchain_huggingface import HuggingFaceEmbeddings
        except ImportError:
            from langchain_community.embeddings import HuggingFaceEmbeddings
        _embedding_fn = HuggingFaceEmbeddings(
            model_name='all-MiniLM-L6-v2',
            model_kwargs={'device': 'cpu'},
        )
    return _embedding_fn


def _index_path(classroom_id: int) -> Path:
    return Path(RAG_INDEX_DIR) / str(classroom_id)


def is_indexed(classroom_id: int) -> bool:
    return classroom_id in _vector_stores or _index_path(classroom_id).exists()


def get_training_status(classroom_id: int) -> str:
    if classroom_id in _vector_stores:
        return 'done'
    if _index_path(classroom_id).exists():
        return 'done'
    return _training_status.get(classroom_id, 'idle')


def _nuke_stale_index(classroom_id: int):
    """Delete old on-disk FAISS index + evict in-memory store."""
    _vector_stores.pop(classroom_id, None)
    idx_path = _index_path(classroom_id)
    if idx_path.exists():
        shutil.rmtree(str(idx_path), ignore_errors=True)
        print(f'[RAG] Deleted stale index at {idx_path}')


def build_rag_index(classroom_id: int) -> dict:
    """
    (Re-)build the FAISS index for a classroom.

    CRITICAL FIX: On every call we DELETE the existing index folder first so
    stale embeddings from a previous broken run are never reused.  Then we
    re-extract every PDF through the 3-layer waterfall and rebuild from scratch.
    """
    lecture_dir = Path(LECTURES_BASE_DIR) / str(classroom_id)
    if not lecture_dir.exists():
        return {'ok': False, 'error': 'No lecture files found. Upload PDFs first.'}

    all_files = (
        list(lecture_dir.glob('*.pdf')) +
        list(lecture_dir.glob('*.txt')) +
        list(lecture_dir.glob('*.doc')) +
        list(lecture_dir.glob('*.docx'))
    )

    if not all_files:
        return {'ok': False, 'error': 'No lecture files found. Upload PDFs first.'}

    # ── Nuke old index NOW (before thread) so it never loads again ─────
    _nuke_stale_index(classroom_id)
    _training_status[classroom_id] = 'running'

    def _build():
        try:
            # ── FIX #1: import Document from langchain_core directly ────
            # 'from langchain.schema import Document' triggers a broken
            # import of langchain_core.memory on newer langchain installs.
            # langchain_core.documents is always safe.
            from langchain_core.documents import Document
            from langchain.text_splitter import RecursiveCharacterTextSplitter
            from langchain_community.vectorstores import FAISS

            emb = _get_embedding_fn()
            docs: list = []

            # ── Extract PDFs via 3-layer waterfall ─────────────────────
            for pdf_file in lecture_dir.glob('*.pdf'):
                text = _extract_pdf_text(str(pdf_file))
                if not text.strip():
                    print(f'[RAG] SKIP {pdf_file.name} — no text extracted')
                    continue

                # Inject the lecture filename at the TOP of the text so the
                # embedder sees it in every chunk and the LLM can cite it.
                lecture_label = f'[SOURCE: {pdf_file.name}]\n'
                docs.append(Document(
                    page_content=lecture_label + text,
                    metadata={
                        'source':   str(pdf_file),
                        'filename': pdf_file.name,
                    }
                ))
                print(f'[RAG] Loaded {pdf_file.name}: {len(text)} chars')

            # ── Load .txt files ────────────────────────────────────────
            for txt_file in lecture_dir.glob('*.txt'):
                try:
                    text = txt_file.read_text(encoding='utf-8', errors='replace').strip()
                    if text:
                        docs.append(Document(
                            page_content=f'[SOURCE: {txt_file.name}]\n' + text,
                            metadata={'source': str(txt_file), 'filename': txt_file.name}
                        ))
                except Exception:
                    pass

            if not docs:
                _training_status[classroom_id] = 'error'
                print(f'[RAG] ERROR: No text extracted from any file in {lecture_dir}.')
                print('[RAG] Your PDFs may be image-only (scanned slides). '
                      'Try uploading the PDF exported with "Save as PDF with text".')
                return

            total_chars = sum(len(d.page_content) for d in docs)
            print(f'[RAG] Total text: {total_chars} chars from {len(docs)} document(s)')

            # ── Chunk ──────────────────────────────────────────────────
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=CHUNK_SIZE,
                chunk_overlap=CHUNK_OVERLAP,
                separators=['\n\n', '\n', '. ', ' ', ''],
            )
            chunks = splitter.split_documents(docs)

            # Ensure each chunk keeps the filename metadata
            for chunk in chunks:
                if 'filename' not in chunk.metadata:
                    chunk.metadata['filename'] = 'unknown'

            print(f'[RAG] Created {len(chunks)} chunks from {len(docs)} documents')

            # ── Build & save FAISS index ───────────────────────────────
            store = FAISS.from_documents(chunks, emb)
            _vector_stores[classroom_id] = store

            idx_path = _index_path(classroom_id)
            idx_path.mkdir(parents=True, exist_ok=True)
            store.save_local(str(idx_path))

            _training_status[classroom_id] = 'done'
            print(
                f'[RAG] ✓ Index built for classroom {classroom_id}: '
                f'{len(chunks)} chunks → {idx_path}'
            )
        except Exception as e:
            _training_status[classroom_id] = 'error'
            print(f'[RAG] Build FAILED for classroom {classroom_id}: {e}')
            import traceback; traceback.print_exc()

    t = threading.Thread(target=_build, daemon=True)
    t.start()
    return {
        'ok': True,
        'message': f'Training started for {len(all_files)} file(s). '
                   'Refresh in ~30 seconds.'
    }


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


_conversation_memory: dict = {}


def rag_query(
    classroom_id: int,
    question: str,
    k: int = RAG_K,
    context_data: Optional[dict] = None,
    session_key: Optional[str] = None,
) -> str:
    """
    Answer a question using MMR retrieval over the classroom FAISS index.
    Falls back gracefully to general knowledge when no index exists.
    """
    from langchain_core.messages import HumanMessage, AIMessage
    llm = get_llm()

    assign_ctx = ''
    if context_data and context_data.get('assignments'):
        lines = ['Upcoming assignments:']
        for a in context_data['assignments']:
            lines.append(f"  - {a['title']} | Due: {a['due_date']} | Max marks: {a['max_marks']}")
        assign_ctx = '\n'.join(lines)

    class_name   = (context_data or {}).get('class_name', '')
    subject      = (context_data or {}).get('subject', '')
    student_name = (context_data or {}).get('student_name', '')

    if classroom_id not in _vector_stores:
        _load_rag_index(classroom_id)

    context_chunks = ''
    source_files   = []

    if classroom_id in _vector_stores:
        try:
            store        = _vector_stores[classroom_id]
            total_chunks = store.index.ntotal
            actual_fetch = min(MMR_FETCH_K, total_chunks)
            actual_k     = min(k, actual_fetch)

            docs = store.max_marginal_relevance_search(
                question,
                k=actual_k,
                fetch_k=actual_fetch,
                lambda_mult=MMR_LAMBDA,
            )

            context_chunks = '\n\n---\n\n'.join(d.page_content for d in docs)
            source_files = list(dict.fromkeys(
                d.metadata.get('filename', d.metadata.get('source', ''))
                for d in docs
            ))
            print(
                f'[RAG] MMR: classroom={classroom_id} fetch={actual_fetch} '
                f'→ kept={actual_k} from {source_files}'
            )
        except Exception as e:
            print(f'[RAG] MMR error: {e} — falling back to similarity search')
            try:
                store       = _vector_stores[classroom_id]
                actual_k    = min(k, store.index.ntotal)
                retriever   = store.as_retriever(search_kwargs={'k': actual_k})
                docs        = retriever.invoke(question)
                context_chunks = '\n\n---\n\n'.join(d.page_content for d in docs)
            except Exception as e2:
                print(f'[RAG] Fallback also failed: {e2}')

    history_msgs = []
    if session_key:
        history_msgs = _conversation_memory.get(session_key, [])[-10:]

    if context_chunks:
        sources_str = ', '.join(source_files) if source_files else 'course materials'
        system_prompt = (
            f"You are a knowledgeable and helpful AI teaching assistant for the "
            f"'{class_name}' ({subject}) classroom on NeuroClass.\n"
            f"Student's name: {student_name}\n\n"
            f"{assign_ctx}\n\n"
            "INSTRUCTIONS:\n"
            "1. The COURSE MATERIAL below was retrieved from the uploaded lecture notes "
            f"({sources_str}).\n"
            "2. Use this material as your PRIMARY source of truth.\n"
            "3. If the material covers the topic partially, supplement with general "
            "knowledge and clearly say '[General knowledge]:' before that part.\n"
            "4. If the material does NOT cover the topic, answer from general knowledge "
            "and say '[Note: Answer based on general knowledge — not in the lecture notes]'.\n"
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

    if session_key:
        mem = _conversation_memory.get(session_key, [])
        mem.append(HumanMessage(content=question))
        mem.append(AIMessage(content=answer))
        _conversation_memory[session_key] = mem[-20:]

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
            text = _extract_pdf_text(str(p))
            state['extracted_text'] = text if text else 'ERROR: No text extracted'
            print(f'[Grader] Extracted {len(text)} chars')
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
            state.update(evaluation='ERROR', score=0.0,
                         feedback='Could not read file. Score: 0.', locked=True)
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
            "CRITERION_BREAKDOWN:\n- criterion: score/max — reason\n"
            "SCORE: 0-100\nGRADE: A/B/C/D/F\n"
            "STRENGTHS: ...\nWEAKNESSES: ...\n"
            "IMPROVEMENT_SUGGESTIONS:\n- ...\nDETAILED_FEEDBACK: ..."
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
        grade = ('A' if state['score'] >= 90 else 'B' if state['score'] >= 80
                 else 'C' if state['score'] >= 70 else 'D' if state['score'] >= 60 else 'F')
        print(f"[Grader] LOCKED student={state['student_id']} "
              f"score={state['score']} grade={grade}")
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


def evaluate_assignment(
    submission_pdf: str,
    rubric: str,
    course_id: str,
    student_id: str,
) -> dict:
    chain = get_assignment_chain()
    return chain.invoke({
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


# ═══════════════════════════════════════════════════════════
#  PROJECT GRADER  (from notebook cells 5 / 5b)
# ═══════════════════════════════════════════════════════════

def _clone_or_pull(repo_url: str, local_path: str) -> bool:
    import subprocess
    p = Path(local_path)
    cmd = (['git', '-C', local_path, 'pull']
           if p.exists() else ['git', 'clone', repo_url, local_path])
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
    files = [f.replace(local_path, '').lstrip('/')
             for f in r.stdout.strip().split('\n') if f][:60]
    parts.append('FILE STRUCTURE (sample):\n' + '\n'.join(files))
    r = subprocess.run(
        ['git', '-C', local_path, 'log',
         '--oneline', '--format=%h %ad %s', '--date=short', '-20'],
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
    lines = {ln.split(':')[0].strip().upper(): ln.split(':', 1)[1].strip()
             for ln in response.splitlines() if ':' in ln}
    has_code    = 'YES' in lines.get('Q1_HAS_CODE',   '').upper()
    is_relevant = 'YES' in lines.get('Q2_IS_RELEVANT', '').upper()
    reason      = lines.get('REASON', response[:200])
    return {'relevant': has_code and is_relevant,
            'has_code': has_code, 'is_relevant': is_relevant, 'reason': reason}


def evaluate_project(
    repo_url: str,
    project_rubric: str,
    project_details: str,
    student_id: str,
    classroom_id: int,
) -> dict:
    from langchain_core.messages import HumanMessage
    local_path = str(
        Path(Config.UPLOAD_FOLDER) / 'student_repos' / str(classroom_id) / str(student_id)
    )
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)

    if not _clone_or_pull(repo_url, local_path):
        return {'error': f'Could not clone {repo_url}. Ensure the repo is public.'}

    summary = _get_repo_summary(local_path)
    rel     = _check_repo_relevance(summary, project_details, project_rubric)

    if not rel['relevant']:
        reason_type = 'EMPTY_OR_NO_CODE' if not rel['has_code'] else 'WRONG_PROJECT'
        msg = ('No meaningful code found.' if not rel['has_code']
               else 'Repo not related to the project.')
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
    grade = ('A' if score >= 90 else 'B' if score >= 80
             else 'C' if score >= 70 else 'D' if score >= 60 else 'F')
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
    from langchain_core.messages import HumanMessage
    local_path = str(
        Path(Config.UPLOAD_FOLDER) / 'student_repos' / str(classroom_id) / f'{student_id}_advisory'
    )
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
