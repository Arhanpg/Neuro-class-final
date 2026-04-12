"""
NeuroClass AI Engine
====================
Provides:
  - FallbackLLM : Gemini → OpenRouter → Groq cascade
  - build_rag_index(classroom_id) : embed all PDFs in lectures/<classroom_id>/
  - rag_query(classroom_id, question, context_data) : RAG answer
  - is_indexed(classroom_id) : check if FAISS index exists
"""

import os
import re
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

# ── LLM providers ──────────────────────────────────────────────────────────
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage

# ── RAG ────────────────────────────────────────────────────────────────────
from langchain_community.document_loaders import PyPDFDirectoryLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import HuggingFaceEmbeddings

# ── Config ──────────────────────────────────────────────────────────────────
LECTURES_BASE = os.getenv('UPLOAD_FOLDER', 'uploads') + '/lectures'
INDEX_BASE    = os.getenv('UPLOAD_FOLDER', 'uploads') + '/rag_indexes'

FALLBACK_TRIGGERS = [
    'quota', 'rate', 'limit', '429', 'exceeded', 'not found', '404',
    'no endpoints', 'unavailable', 'overloaded', '503', 'capacity',
    'decommissioned', 'deprecated', 'notfound', 'invalid_api_key',
    'authentication', 'modelnotfound', 'auth', 'permission',
    'unauthorized', '400', '401', '403', 'api key not valid',
]

# Shared embeddings model (loaded once)
_embedding_fn = None
_vector_stores: dict = {}   # classroom_id -> FAISS store


def _get_embeddings():
    global _embedding_fn
    if _embedding_fn is None:
        _embedding_fn = HuggingFaceEmbeddings(
            model_name='all-MiniLM-L6-v2',
            model_kwargs={'device': 'cpu'},
        )
    return _embedding_fn


# ── Fallback LLM ────────────────────────────────────────────────────────────
class FallbackLLM:
    """Gemini-2.0-Flash → OpenRouter Llama-3.3-70B → Groq Llama-3.3-70B → Groq Llama-3.1-8B"""

    def __init__(self):
        self.models: list = []
        self.active_idx = 0
        self._init_models()

    def _init_models(self):
        gemini_key = os.getenv('GEMINI_API_KEY', '')
        or_key     = os.getenv('OPENROUTER_API_KEY', '')
        groq_key   = os.getenv('GROQ_API_KEY', '')

        if gemini_key:
            try:
                self.models.append(('Gemini-2.0-Flash', ChatGoogleGenerativeAI(
                    model='gemini-2.0-flash', google_api_key=gemini_key, temperature=0.2
                )))
            except Exception:
                pass

        if or_key:
            try:
                self.models.append(('OpenRouter-Llama-3.3-70B', ChatOpenAI(
                    model='meta-llama/llama-3.3-70b-instruct:free',
                    openai_api_base='https://openrouter.ai/api/v1',
                    openai_api_key=or_key,
                    temperature=0.2,
                    default_headers={
                        'HTTP-Referer': 'https://neuroclass.app',
                        'X-Title': 'NeuroClass',
                    },
                )))
            except Exception:
                pass

        if groq_key:
            try:
                self.models.append(('Groq-Llama-3.3-70B', ChatGroq(
                    model='llama-3.3-70b-versatile', api_key=groq_key, temperature=0.2
                )))
            except Exception:
                pass
            try:
                self.models.append(('Groq-Llama-3.1-8B', ChatGroq(
                    model='llama-3.1-8b-instant', api_key=groq_key, temperature=0.2
                )))
            except Exception:
                pass

        if not self.models:
            raise RuntimeError(
                'No LLM provider initialised. '
                'Set at least one of GEMINI_API_KEY / OPENROUTER_API_KEY / GROQ_API_KEY in .env'
            )

    def invoke(self, messages) -> str:
        for idx in range(self.active_idx, len(self.models)):
            name, model = self.models[idx]
            try:
                result = model.invoke(messages)
                if idx != self.active_idx:
                    self.active_idx = idx
                return result.content
            except Exception as e:
                err = str(e).lower()
                if any(t in err for t in FALLBACK_TRIGGERS):
                    continue
                raise RuntimeError(f'LLM error ({name}): {e}')
        raise RuntimeError('All LLM providers failed.')

    @property
    def current_name(self):
        return self.models[self.active_idx][0] if self.models else 'none'


# Singleton – instantiated lazily so the Flask app can import this module
# even before .env is loaded.
_llm: FallbackLLM | None = None


def get_llm() -> FallbackLLM:
    global _llm
    if _llm is None:
        _llm = FallbackLLM()
    return _llm


# ── RAG helpers ─────────────────────────────────────────────────────────────

def _safe_name(classroom_id) -> str:
    return re.sub(r'[^\w]', '_', str(classroom_id))


def _lecture_dir(classroom_id) -> Path:
    return Path(LECTURES_BASE) / _safe_name(classroom_id)


def _index_dir(classroom_id) -> Path:
    return Path(INDEX_BASE) / _safe_name(classroom_id)


def is_indexed(classroom_id) -> bool:
    """Return True if a FAISS index exists for this classroom."""
    idx = _index_dir(classroom_id)
    return idx.exists() and (idx / 'index.faiss').exists()


def build_rag_index(classroom_id) -> dict:
    """
    Embed all PDFs in the classroom's lecture folder and persist the FAISS index.
    Returns {'ok': True, 'pages': N, 'chunks': M} or {'ok': False, 'error': '...'}
    """
    folder = _lecture_dir(classroom_id)
    if not folder.exists():
        return {'ok': False, 'error': f'Lecture folder not found: {folder}'}

    pdf_files = list(folder.glob('*.pdf'))
    if not pdf_files:
        return {'ok': False, 'error': 'No PDF files found in lecture folder.'}

    loader = PyPDFDirectoryLoader(str(folder), glob='*.pdf', silent_errors=True)
    docs = loader.load()
    if not docs:
        return {'ok': False, 'error': 'PDFs found but no text could be extracted (scanned images?)'}

    splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=120)
    chunks = splitter.split_documents(docs)

    emb = _get_embeddings()
    store = FAISS.from_documents(chunks, emb)

    idx_path = _index_dir(classroom_id)
    idx_path.mkdir(parents=True, exist_ok=True)
    store.save_local(str(idx_path))

    _vector_stores[classroom_id] = store
    return {'ok': True, 'pages': len(docs), 'chunks': len(chunks), 'files': len(pdf_files)}


def _load_index(classroom_id) -> bool:
    """Load index from disk into memory. Returns True on success."""
    idx_path = _index_dir(classroom_id)
    if not idx_path.exists():
        return False
    emb = _get_embeddings()
    _vector_stores[classroom_id] = FAISS.load_local(
        str(idx_path), emb, allow_dangerous_deserialization=True
    )
    return True


def rag_query(classroom_id, question: str, context_data: dict | None = None) -> str:
    """
    Answer a student question using RAG over the classroom's lecture notes.
    context_data can contain: assignments (list), deadlines (list) for richer answers.
    Returns plain-text answer.
    """
    # Load index if not in memory
    if classroom_id not in _vector_stores and not _load_index(classroom_id):
        # No RAG index – fall back to pure LLM with context_data
        context_text = ''
    else:
        retriever = _vector_stores[classroom_id].as_retriever(search_kwargs={'k': 5})
        docs = retriever.invoke(question)
        context_text = '\n\n'.join(d.page_content for d in docs)

    # Build extra context string from structured data (assignments, deadlines etc.)
    extra = ''
    if context_data:
        if context_data.get('assignments'):
            extra += '\n\n--- ASSIGNMENTS / DEADLINES ---\n'
            for a in context_data['assignments']:
                extra += (
                    f"Assignment: {a.get('title')} | "
                    f"Due: {a.get('due_date', 'Not set')} | "
                    f"Description: {a.get('description', '')}\n"
                )

    system_msg = """
You are NeuroBot, a smart classroom assistant for NeuroClass.
Your job is to help students with:
  1. Questions about course material (answer ONLY from the provided lecture notes context).
  2. General academic queries (study plans, tips, explanations) — use your own knowledge.
  3. Classroom-specific info (assignments, deadlines) — use the provided structured data.

Rules:
  - If the answer is in the lecture notes context, cite it accurately.
  - If the question is not covered in the lecture notes, say so honestly and still try to help with general knowledge.
  - For assignment deadlines or details, refer to the ASSIGNMENTS section if provided.
  - Be concise, friendly, and encouraging.
  - Format your answer clearly with bullet points or short paragraphs.
"""

    prompt = f"{system_msg}\n\n"
    if context_text:
        prompt += f"LECTURE NOTES CONTEXT:\n{context_text}\n\n"
    if extra:
        prompt += extra + '\n'
    prompt += f"STUDENT QUESTION:\n{question}\n\nANSWER:"

    try:
        llm = get_llm()
        return llm.invoke([HumanMessage(content=prompt)])
    except Exception as e:
        return f"Sorry, I couldn't process your question right now. Error: {e}"
