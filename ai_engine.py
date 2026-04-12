"""
NeuroClass AI Engine
====================
RAG (Retrieval-Augmented Generation) + Fallback LLM manager.
Background training via threading so the Flask server stays responsive.

v2 improvements:
 - Strongly grounded system prompt — AI answers from lecture notes first
 - Conversation memory per session (last N turns)
 - Lecture-file-aware retrieval (can answer "what is in Lecture 9")
 - Multi-query retrieval for better coverage
 - Source citations shown in responses (file name + page)
 - Smarter fallback — clearly labels when using general knowledge
 - Student name memory within a session
"""

import os
import threading
import warnings
from pathlib import Path
from collections import deque
from typing import List, Dict, Optional

# Suppress noisy PDF float-parsing warnings from pypdf
warnings.filterwarnings('ignore', message='.*FloatObject.*')
warnings.filterwarnings('ignore', message='.*could not convert string to float.*')

# ── Module-level singletons ────────────────────────────────────────────────
_llm_manager = None
_embedding_fn = None
_vector_stores: dict = {}   # classroom_id -> FAISS store
_lecture_index: dict = {}   # classroom_id -> {filename: [page_nums]}

# Conversation memory: session_key -> deque of {"role": "user"|"assistant", "content": str}
# session_key = f"{classroom_id}_{user_id}"
_chat_memory: dict = {}
MEMORY_WINDOW = 8  # keep last 8 turns (4 exchanges) per session

# Training state tracker: classroom_id -> 'idle' | 'running' | 'done' | 'error:<msg>'
_training_status: dict = {}
_training_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────
# Memory helpers
# ─────────────────────────────────────────────────────────────────────────

def get_memory(session_key: str) -> deque:
    if session_key not in _chat_memory:
        _chat_memory[session_key] = deque(maxlen=MEMORY_WINDOW)
    return _chat_memory[session_key]


def append_memory(session_key: str, role: str, content: str):
    mem = get_memory(session_key)
    mem.append({"role": role, "content": content})


def format_memory_for_prompt(session_key: str) -> str:
    mem = get_memory(session_key)
    if not mem:
        return ""
    lines = []
    for turn in mem:
        prefix = "Student" if turn["role"] == "user" else "AI"
        lines.append(f"{prefix}: {turn['content']}")
    return "\n".join(lines)


def clear_memory(session_key: str):
    if session_key in _chat_memory:
        del _chat_memory[session_key]


# ─────────────────────────────────────────────────────────────────────────
# Embedding & LLM singletons
# ─────────────────────────────────────────────────────────────────────────

def _get_embedding_fn():
    global _embedding_fn
    if _embedding_fn is None:
        try:
            from langchain_huggingface import HuggingFaceEmbeddings
        except ImportError:
            from langchain_community.embeddings import HuggingFaceEmbeddings  # type: ignore
        _embedding_fn = HuggingFaceEmbeddings(
            model_name='all-MiniLM-L6-v2',
            model_kwargs={'device': 'cpu'},
        )
    return _embedding_fn


def _get_llm_manager():
    global _llm_manager
    if _llm_manager is None:
        _llm_manager = FallbackLLM()
    return _llm_manager


# ─────────────────────────────────────────────────────────────────────────
# FallbackLLM: Gemini → OpenRouter → Groq-70B → Groq-8B
# ─────────────────────────────────────────────────────────────────────────
class FallbackLLM:
    FALLBACK_TRIGGERS = (
        'quota', 'rate', 'limit', '429', 'exceeded', 'not found',
        '404', 'no endpoints', 'unavailable', 'overloaded', '503',
        'capacity', 'decommissioned', 'deprecated', 'notfound',
        'invalid api key', 'api key not valid', 'auth', 'permission',
        'unauthorized', '400', '401', '403', 'modelnotfound',
    )

    def __init__(self):
        self.models = []
        self.active_idx = 0
        self._init_models()

    def _init_models(self):
        from config import Config

        # 1. Gemini 2.0 Flash
        if Config.GEMINI_API_KEY:
            try:
                from langchain_google_genai import ChatGoogleGenerativeAI
                self.models.append(('Gemini-2.0-Flash', ChatGoogleGenerativeAI(
                    model='gemini-2.0-flash',
                    google_api_key=Config.GEMINI_API_KEY,
                    temperature=0.4,
                )))
            except Exception as e:
                print(f'[AI] Gemini init failed: {e}')

        # 2. OpenRouter Llama-3.3-70B (free)
        if Config.OPENROUTER_API_KEY:
            try:
                from langchain_openai import ChatOpenAI
                self.models.append(('OpenRouter/Llama-3.3-70B', ChatOpenAI(
                    model='meta-llama/llama-3.3-70b-instruct:free',
                    openai_api_base='https://openrouter.ai/api/v1',
                    openai_api_key=Config.OPENROUTER_API_KEY,
                    temperature=0.4,
                    default_headers={
                        'HTTP-Referer': 'https://neuroclass.app',
                        'X-Title': 'NeuroClass',
                    },
                )))
            except Exception as e:
                print(f'[AI] OpenRouter init failed: {e}')

        # 3. Groq Llama-3.3-70B
        if Config.GROQ_API_KEY:
            try:
                from langchain_groq import ChatGroq
                self.models.append(('Groq/Llama-3.3-70B', ChatGroq(
                    model='llama-3.3-70b-versatile',
                    api_key=Config.GROQ_API_KEY,
                    temperature=0.4,
                )))
            except Exception as e:
                print(f'[AI] Groq-70B init failed: {e}')

            # 4. Groq Llama-3.1-8B Instant (fast fallback)
            try:
                from langchain_groq import ChatGroq
                self.models.append(('Groq/Llama-3.1-8B', ChatGroq(
                    model='llama-3.1-8b-instant',
                    api_key=Config.GROQ_API_KEY,
                    temperature=0.4,
                )))
            except Exception as e:
                print(f'[AI] Groq-8B init failed: {e}')

        if not self.models:
            print('[AI] WARNING: No LLM provider initialised. Check your .env API keys.')

    def invoke(self, prompt: str) -> str:
        """Try each provider in order; fall back on quota/rate errors."""
        if not self.models:
            return (
                '⚠️ No AI provider is configured. '
                'Please add your API keys to the .env file.'
            )

        from langchain_core.messages import HumanMessage
        messages = [HumanMessage(content=prompt)]

        for idx in range(self.active_idx, len(self.models)):
            name, model = self.models[idx]
            try:
                result = model.invoke(messages)
                if idx != self.active_idx:
                    print(f'[AI] Switched to {name}')
                    self.active_idx = idx
                return result.content if hasattr(result, 'content') else str(result)
            except Exception as e:
                err = str(e).lower()
                if any(t in err for t in self.FALLBACK_TRIGGERS):
                    print(f'[AI] {name} unavailable ({str(e)[:60]}), trying next...')
                    continue
                raise

        return '⚠️ All AI providers are currently unavailable. Please try again later.'

    @property
    def current_name(self):
        return self.models[self.active_idx][0] if self.models else 'None'


# ─────────────────────────────────────────────────────────────────────────
# RAG path helpers
# ─────────────────────────────────────────────────────────────────────────

def _lecture_path(classroom_id: int) -> Path:
    from config import Config
    return Path(Config.LECTURES_BASE_DIR) / str(classroom_id)


def _index_path(classroom_id: int) -> Path:
    from config import Config
    return Path(Config.RAG_INDEX_DIR) / str(classroom_id)


def is_indexed(classroom_id: int) -> bool:
    ip = _index_path(classroom_id)
    return (ip / 'index.faiss').exists()


def get_training_status(classroom_id: int) -> str:
    return _training_status.get(classroom_id, 'idle')


# ─────────────────────────────────────────────────────────────────────────
# Build lecture file index (filename → pages map) for "what's in Lecture 9"
# ─────────────────────────────────────────────────────────────────────────

def _build_lecture_index(classroom_id: int, docs: list):
    """
    Build a map of {normalized_filename: list_of_page_numbers}
    from the loaded documents so the AI can answer file-specific questions.
    """
    index = {}
    for doc in docs:
        src = doc.metadata.get('source', '')
        page = doc.metadata.get('page', None)
        fname = Path(src).stem.lower().replace('_', ' ').replace('-', ' ')  # e.g. "lecture 9"
        if fname not in index:
            index[fname] = []
        if page is not None and page not in index[fname]:
            index[fname].append(page)
    _lecture_index[classroom_id] = index
    return index


def _get_lecture_list(classroom_id: int) -> str:
    """Returns a human-readable list of indexed lecture files."""
    index = _lecture_index.get(classroom_id, {})
    if not index:
        return 'No lecture files are indexed yet.'
    lines = []
    for fname, pages in sorted(index.items()):
        page_info = f"({len(pages)} pages)" if pages else ""
        lines.append(f"• {fname.title()} {page_info}")
    return '\n'.join(lines)


# ─────────────────────────────────────────────────────────────────────────
# Multi-query retrieval for better coverage
# ─────────────────────────────────────────────────────────────────────────

def _multi_query_retrieve(store, question: str, k: int = 6) -> List:
    """
    Generate 2 query variants and combine unique results.
    This catches cases where the exact phrasing misses relevant chunks.
    """
    queries = [question]

    # Simple keyword expansion — extract core noun phrases
    q_lower = question.lower()
    if 'lecture' in q_lower and any(c.isdigit() for c in question):
        # e.g. "what is in lecture 9" → also search "lecture 9 topics"
        queries.append(question + " topics content")
    elif '?' in question:
        queries.append(question.replace('?', '').strip())
    else:
        queries.append(question + " explain")

    seen_ids = set()
    all_docs = []
    retriever = store.as_retriever(search_kwargs={'k': k})
    for q in queries:
        try:
            results = retriever.invoke(q)
            for doc in results:
                doc_id = doc.page_content[:80]
                if doc_id not in seen_ids:
                    seen_ids.add(doc_id)
                    all_docs.append(doc)
        except Exception as e:
            print(f'[AI] Retrieval query failed: {e}')

    return all_docs[:k + 2]  # cap at k+2 to avoid context overflow


# ─────────────────────────────────────────────────────────────────────────
# Format retrieved docs with source citations
# ─────────────────────────────────────────────────────────────────────────

def _format_context_with_sources(docs: list) -> tuple:
    """
    Returns (context_text, sources_summary)
    context_text: the raw text for the prompt
    sources_summary: e.g. "Lecture-9.pdf (p.3), Lecture-9.pdf (p.5)"
    """
    context_parts = []
    sources = []

    for doc in docs:
        src = doc.metadata.get('source', 'unknown')
        page = doc.metadata.get('page', None)
        fname = Path(src).name if src != 'unknown' else 'course material'
        page_str = f" p.{page + 1}" if page is not None else ""
        label = f"{fname}{page_str}"

        context_parts.append(f"[Source: {label}]\n{doc.page_content}")
        if label not in sources:
            sources.append(label)

    return '\n\n'.join(context_parts), ', '.join(sources) if sources else 'course material'


# ─────────────────────────────────────────────────────────────────────────
# Main RAG query function
# ─────────────────────────────────────────────────────────────────────────

def rag_query(
    classroom_id: int,
    question: str,
    context_data: dict = None,
    session_key: str = None,
) -> str:
    """
    Main entry point for chatbot queries.

    Parameters
    ----------
    classroom_id : int
    question     : str   — the student's question
    context_data : dict  — optional: assignments, class_name, student_name, subject
    session_key  : str   — e.g. "42_7" (classroom_id_user_id) for per-user memory
    """
    llm = _get_llm_manager()
    session_key = session_key or str(classroom_id)

    # ── Build classroom context block ──
    class_name = ''
    subject = ''
    student_name = ''
    assignments_block = ''

    if context_data:
        class_name = context_data.get('class_name', '')
        subject = context_data.get('subject', '')
        student_name = context_data.get('student_name', '')
        assignments = context_data.get('assignments', [])
        if assignments:
            lines = []
            for a in assignments:
                lines.append(
                    f"  - {a.get('title', '?')} "
                    f"(due: {a.get('due_date', 'TBD')}, "
                    f"max marks: {a.get('max_marks', '?')})"
                )
            assignments_block = 'ASSIGNMENTS:\n' + '\n'.join(lines)

    class_block = ''
    if class_name or subject:
        class_block = f"Class: {class_name}" + (f" | Subject: {subject}" if subject else "")

    student_block = ''
    if student_name:
        student_block = f"The student you are talking to is: {student_name}"

    # ── Retrieve conversation history ──
    memory_block = format_memory_for_prompt(session_key)

    # ── Try RAG retrieval ──
    has_index = _load_index(classroom_id)

    if has_index:
        try:
            docs = _multi_query_retrieve(_vector_stores[classroom_id], question, k=7)
            context, sources = _format_context_with_sources(docs)

            # Check if question is about lecture files specifically
            lecture_list_hint = ''
            q_lower = question.lower()
            if 'what' in q_lower and ('lecture' in q_lower or 'topic' in q_lower or 'cover' in q_lower or 'content' in q_lower):
                lecture_list_hint = f"\nINDEXED LECTURE FILES:\n{_get_lecture_list(classroom_id)}"

            prompt = f"""You are NeuroClass AI, a smart and friendly teaching assistant.

YOUR RULES:
1. ALWAYS answer from the LECTURE NOTES provided below first. They are your primary source of truth.
2. If the answer is clearly and fully present in the lecture notes, answer it directly and confidently — do NOT say "I couldn't find" or "the material doesn't mention" when the content IS there.
3. If the lecture notes cover the topic partially, use them as the base and supplement with your knowledge — clearly say "Based on the lecture notes, ... Additionally from general AI knowledge, ..."
4. If the topic is completely absent from the lecture notes (e.g. personal questions, scheduling), use general knowledge and say "This isn't in the lecture notes, but here's what I know: ..."
5. NEVER say the lecture notes are empty or only contain repeated keywords — extract the actual content.
6. Remember previous conversation context when answering follow-up questions.
7. If the student tells you their name, remember it and use it naturally.
8. When quoting from lecture notes, mention the source (e.g. "As covered in Lecture 9...").
9. Be concise, structured, and student-friendly. Use bullet points and bold text where helpful.
10. For algorithm questions, always include: definition, key idea, step-by-step approach, and comparison with related algorithms if relevant.

{f'CLASSROOM: {class_block}' if class_block else ''}
{student_block}
{lecture_list_hint}

LECTURE NOTES (your primary knowledge source):
{context}

{assignments_block}

CONVERSATION HISTORY (for context):
{memory_block if memory_block else 'No previous messages in this session.'}

STUDENT'S QUESTION: {question}

ANSWER (be thorough, cite the lecture source, use the notes above):"""

            answer = llm.invoke(prompt)

            # Append source footer if sources are real files
            if sources and sources != 'course material' and 'pdf' in sources.lower():
                answer = answer.rstrip() + f"\n\n📄 *Sources: {sources}*"

            # Save to memory
            append_memory(session_key, 'user', question)
            append_memory(session_key, 'assistant', answer[:400])  # truncate for memory

            return answer

        except Exception as e:
            print(f'[AI] RAG retrieval error: {e}')
            # Fall through to no-index path

    # ── No index / fallback path ──
    prompt = f"""You are NeuroClass AI, a smart and friendly teaching assistant.
The lecture notes for this classroom have not been uploaded or indexed yet.
Answer from your general knowledge, but be honest that lecture notes aren't available.
If the student asks something personal (like their name), check the conversation history below.
Be helpful, structured, and concise.

{f'CLASSROOM: {class_block}' if class_block else ''}
{student_block}
{assignments_block}

CONVERSATION HISTORY:
{memory_block if memory_block else 'No previous messages.'}

STUDENT'S QUESTION: {question}

ANSWER:"""

    answer = llm.invoke(prompt)

    append_memory(session_key, 'user', question)
    append_memory(session_key, 'assistant', answer[:400])

    return answer


# ─────────────────────────────────────────────────────────────────────────
# Index build (background thread)
# ─────────────────────────────────────────────────────────────────────────

def _run_build_index(classroom_id: int):
    with _training_lock:
        _training_status[classroom_id] = 'running'

    print(f'[AI] Background training started for classroom {classroom_id}...')
    try:
        result = _build_index_sync(classroom_id)
        if result.get('ok'):
            _training_status[classroom_id] = 'done'
            print(f'[AI] Training complete for classroom {classroom_id}: {result["message"]}')
        else:
            _training_status[classroom_id] = f'error:{result.get("error", "unknown")}'
            print(f'[AI] Training failed for classroom {classroom_id}: {result.get("error")}')
    except Exception as e:
        _training_status[classroom_id] = f'error:{str(e)}'
        print(f'[AI] Training exception for classroom {classroom_id}: {e}')

    try:
        from app import mysql
        if 'done' in _training_status.get(classroom_id, ''):
            conn = mysql.connection
            cur = conn.cursor()
            cur.execute('UPDATE classrooms SET rag_indexed=1 WHERE id=%s', (classroom_id,))
            conn.commit()
            print(f'[AI] DB updated rag_indexed=1 for classroom {classroom_id}')
    except Exception as db_err:
        print(f'[AI] DB update skipped: {db_err}')


def _build_index_sync(classroom_id: int) -> dict:
    """Synchronous index build — call from background thread only."""
    lecture_dir = _lecture_path(classroom_id)
    if not lecture_dir.exists():
        return {'ok': False, 'error': 'No lecture folder found. Upload PDFs first.'}

    pdf_files = list(lecture_dir.glob('*.pdf'))
    txt_files = list(lecture_dir.glob('*.txt'))
    doc_files = list(lecture_dir.glob('*.docx')) + list(lecture_dir.glob('*.doc'))
    all_files = pdf_files + txt_files + doc_files

    if not all_files:
        return {'ok': False, 'error': 'No files found. Upload lecture PDFs/DOCs/TXTs first.'}

    try:
        from langchain_community.document_loaders import (
            PyPDFDirectoryLoader, TextLoader, Docx2txtLoader
        )
        from langchain.text_splitter import RecursiveCharacterTextSplitter
        from langchain_community.vectorstores import FAISS

        docs = []

        # Load PDFs — preserve source metadata (filename + page number)
        if pdf_files:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                loader = PyPDFDirectoryLoader(
                    str(lecture_dir), glob='**/*.pdf', silent_errors=True
                )
                pdf_docs = loader.load()
                # Ensure source metadata is set
                for d in pdf_docs:
                    if 'source' not in d.metadata:
                        d.metadata['source'] = str(lecture_dir / 'unknown.pdf')
                docs.extend(pdf_docs)

        # Load TXT files
        for f in txt_files:
            try:
                loaded = TextLoader(str(f), encoding='utf-8').load()
                for d in loaded:
                    d.metadata['source'] = str(f)
                docs.extend(loaded)
            except Exception:
                try:
                    loaded = TextLoader(str(f), encoding='latin-1').load()
                    for d in loaded:
                        d.metadata['source'] = str(f)
                    docs.extend(loaded)
                except Exception as e:
                    print(f'[AI] Skipping {f.name}: {e}')

        # Load DOCX files
        for f in doc_files:
            try:
                loaded = Docx2txtLoader(str(f)).load()
                for d in loaded:
                    d.metadata['source'] = str(f)
                docs.extend(loaded)
            except Exception as e:
                print(f'[AI] Skipping {f.name}: {e}')

        if not docs:
            return {'ok': False, 'error': 'Files found but no text could be extracted (scanned images?)'}

        # Build lecture file index BEFORE chunking (chunks lose page-level metadata)
        _build_lecture_index(classroom_id, docs)

        # Split with smaller chunks + more overlap for better retrieval
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=600,
            chunk_overlap=150,
            separators=['\n\n', '\n', '. ', ' ', ''],
        )
        chunks = splitter.split_documents(docs)

        # Preserve source metadata on chunks
        for chunk in chunks:
            if 'source' not in chunk.metadata:
                chunk.metadata['source'] = 'unknown'

        emb = _get_embedding_fn()
        store = FAISS.from_documents(chunks, emb)

        idx_path = _index_path(classroom_id)
        idx_path.mkdir(parents=True, exist_ok=True)
        store.save_local(str(idx_path))

        _vector_stores[classroom_id] = store

        return {
            'ok': True,
            'message': (
                f'Trained on {len(docs)} pages '
                f'({len(chunks)} chunks) from {len(all_files)} file(s). '
                f'Indexed lectures: {", ".join(sorted(_lecture_index[classroom_id].keys()))}'
            )
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def build_rag_index(classroom_id: int) -> dict:
    """
    PUBLIC API — called from Flask route.
    Starts background training and returns immediately.
    """
    status = get_training_status(classroom_id)
    if status == 'running':
        return {'ok': False, 'error': 'Training is already in progress. Please wait.'}

    lecture_dir = _lecture_path(classroom_id)
    if not lecture_dir.exists() or not any(lecture_dir.iterdir()):
        return {'ok': False, 'error': 'No lecture files found. Upload PDFs first.'}

    _training_status[classroom_id] = 'running'
    t = threading.Thread(
        target=_run_build_index,
        args=(classroom_id,),
        daemon=True,
        name=f'rag-train-{classroom_id}',
    )
    t.start()

    return {
        'ok': True,
        'background': True,
        'message': (
            '🚀 Training started in the background! '
            'You can navigate away — it will keep running. '
            'Check status with the "Check Status" button.'
        )
    }


def _load_index(classroom_id: int) -> bool:
    if classroom_id in _vector_stores:
        return True
    ip = _index_path(classroom_id)
    if not ip.exists():
        return False
    try:
        from langchain_community.vectorstores import FAISS
        emb = _get_embedding_fn()
        _vector_stores[classroom_id] = FAISS.load_local(
            str(ip), emb, allow_dangerous_deserialization=True
        )
        # Rebuild lecture index from loaded store if not already in memory
        if classroom_id not in _lecture_index:
            # We can't rebuild from FAISS directly, so just mark as loaded
            _lecture_index[classroom_id] = {}
        return True
    except Exception:
        return False
