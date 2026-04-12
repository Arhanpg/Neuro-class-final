"""
NeuroClass AI Engine
====================
RAG (Retrieval-Augmented Generation) + Fallback LLM manager.
Mirrors the logic from NeuroClass_v4_Final.ipynb but adapted for
a Flask web server (no CLI, no Colab, no interactive prompts).

All indexes and uploads are stored locally under the paths defined
in config.py (uploads/lectures/<classroom_id> and
uploads/rag_indexes/<classroom_id>).
"""

import os
import re
from pathlib import Path

# ── Lazy imports so the app starts even if heavy deps aren't installed yet ──
_llm_manager = None
_embedding_fn = None
_vector_stores: dict = {}  # classroom_id -> FAISS store


def _get_embedding_fn():
    global _embedding_fn
    if _embedding_fn is None:
        from langchain_community.embeddings import HuggingFaceEmbeddings
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


# ─────────────────────────────────────────────────
# FallbackLLM: Gemini → OpenRouter → Groq-70B → Groq-8B
# ─────────────────────────────────────────────────
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
                    temperature=0.3,
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
                    temperature=0.3,
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
                    temperature=0.3,
                )))
            except Exception as e:
                print(f'[AI] Groq-70B init failed: {e}')

            # 4. Groq Llama-3.1-8B Instant (fast fallback)
            try:
                from langchain_groq import ChatGroq
                self.models.append(('Groq/Llama-3.1-8B', ChatGroq(
                    model='llama-3.1-8b-instant',
                    api_key=Config.GROQ_API_KEY,
                    temperature=0.3,
                )))
            except Exception as e:
                print(f'[AI] Groq-8B init failed: {e}')

        if not self.models:
            print('[AI] WARNING: No LLM provider initialised. Check your .env API keys.')

    def invoke(self, messages) -> str:
        """Try each provider in order; fall back on quota/rate errors."""
        if not self.models:
            return ('⚠️ No AI provider is configured. '
                    'Please add your API keys to the .env file.')

        from langchain_core.messages import HumanMessage
        if isinstance(messages, str):
            messages = [HumanMessage(content=messages)]

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


# ─────────────────────────────────────────────────
# RAG helpers
# ─────────────────────────────────────────────────

def _safe_id(classroom_id: int) -> str:
    """Convert classroom_id to a filesystem-safe string."""
    return str(classroom_id)


def _lecture_path(classroom_id: int) -> Path:
    from config import Config
    return Path(Config.LECTURES_BASE_DIR) / _safe_id(classroom_id)


def _index_path(classroom_id: int) -> Path:
    from config import Config
    return Path(Config.RAG_INDEX_DIR) / _safe_id(classroom_id)


def is_indexed(classroom_id: int) -> bool:
    """Return True if a FAISS index exists on disk for this classroom."""
    ip = _index_path(classroom_id)
    return (ip / 'index.faiss').exists()


def build_rag_index(classroom_id: int) -> dict:
    """
    Load all PDFs from the classroom's lecture folder,
    chunk → embed → FAISS → save to disk.
    Returns {'ok': bool, 'message': str}
    """
    lecture_dir = _lecture_path(classroom_id)
    if not lecture_dir.exists():
        return {'ok': False, 'error': 'No lecture folder found. Upload PDFs first.'}

    pdf_files = list(lecture_dir.glob('*.pdf'))
    if not pdf_files:
        return {'ok': False, 'error': 'No PDF files found in the lecture folder.'}

    try:
        from langchain_community.document_loaders import PyPDFDirectoryLoader
        from langchain.text_splitter import RecursiveCharacterTextSplitter
        from langchain_community.vectorstores import FAISS

        loader = PyPDFDirectoryLoader(str(lecture_dir), glob='**/*.pdf', silent_errors=True)
        docs = loader.load()
        if not docs:
            return {'ok': False, 'error': 'PDFs found but no text could be extracted (scanned images?)'}

        splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=120)
        chunks = splitter.split_documents(docs)

        emb = _get_embedding_fn()
        store = FAISS.from_documents(chunks, emb)

        idx_path = _index_path(classroom_id)
        idx_path.mkdir(parents=True, exist_ok=True)
        store.save_local(str(idx_path))

        _vector_stores[classroom_id] = store

        return {
            'ok': True,
            'message': f'✅ AI trained on {len(docs)} pages ({len(chunks)} chunks) from {len(pdf_files)} PDF(s).'
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _load_index(classroom_id: int) -> bool:
    """Load FAISS index from disk into memory. Returns True on success."""
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
        return True
    except Exception:
        return False


def rag_query(classroom_id: int, question: str, context_data: dict = None) -> str:
    """
    Answer a student question using RAG on the class notes.
    Falls back to general LLM if no index is available.
    context_data can include {'assignments': [...], 'deadlines': {...}}
    """
    llm = _get_llm_manager()

    # Build extra context string from structured data (assignments, deadlines, etc.)
    extra_ctx = ''
    if context_data:
        if context_data.get('assignments'):
            extra_ctx += '\n\nASSIGNMENTS:\n'
            for a in context_data['assignments']:
                extra_ctx += f"- {a.get('title','?')} (due: {a.get('due_date','TBD')})\n"
        if context_data.get('class_name'):
            extra_ctx += f"\nClass: {context_data['class_name']}\n"

    # Try RAG if index exists
    if _load_index(classroom_id):
        try:
            retriever = _vector_stores[classroom_id].as_retriever(
                search_kwargs={'k': 5}
            )
            docs = retriever.invoke(question)
            context = '\n\n'.join(d.page_content for d in docs)

            prompt = f"""You are a helpful AI teaching assistant for NeuroClass.
Answer the student's question using the course material below as your primary source.
If the answer is not in the material, use your general knowledge and say so honestly.
Always be helpful, clear, and concise.

COURSE MATERIAL:
{context}
{extra_ctx}
STUDENT QUESTION: {question}

ANSWER:"""
            return llm.invoke(prompt)
        except Exception as e:
            # Fall through to general answer
            print(f'[AI] RAG retrieval error: {e}')

    # No index — general LLM answer
    prompt = f"""You are a helpful AI teaching assistant for NeuroClass.
The course materials have not been indexed yet, so answer from general knowledge.
Be helpful, clear, and concise.
{extra_ctx}
STUDENT QUESTION: {question}

ANSWER:"""
    return llm.invoke(prompt)
