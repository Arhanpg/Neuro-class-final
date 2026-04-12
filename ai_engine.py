"""
NeuroClass AI Engine
====================
RAG (Retrieval-Augmented Generation) + Fallback LLM manager.
Background training via threading so the Flask server stays responsive.
"""

import os
import threading
import warnings
from pathlib import Path

# Suppress noisy PDF float-parsing warnings from pypdf
warnings.filterwarnings('ignore', message='.*FloatObject.*')
warnings.filterwarnings('ignore', message='.*could not convert string to float.*')

# ── Module-level singletons ────────────────────────────────────────────────
_llm_manager = None
_embedding_fn = None
_vector_stores: dict = {}  # classroom_id -> FAISS store

# Training state tracker: classroom_id -> 'idle' | 'running' | 'done' | 'error'
_training_status: dict = {}
_training_lock = threading.Lock()


def _get_embedding_fn():
    global _embedding_fn
    if _embedding_fn is None:
        try:
            # Prefer the non-deprecated langchain-huggingface package
            from langchain_huggingface import HuggingFaceEmbeddings
        except ImportError:
            # Fall back to langchain-community if not installed yet
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


# ─────────────────────────────────────────────────────────────────────────
# RAG helpers
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
    """Returns: 'idle' | 'running' | 'done' | 'error:<msg>'"""
    return _training_status.get(classroom_id, 'idle')


def _run_build_index(classroom_id: int):
    """
    Internal function that runs in a background thread.
    Updates _training_status during the process.
    """
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

    # Update the DB rag_indexed flag after training
    try:
        from app import mysql
        import MySQLdb.cursors
        if 'done' in _training_status.get(classroom_id, ''):
            conn = mysql.connection
            cur = conn.cursor()
            cur.execute('UPDATE classrooms SET rag_indexed=1 WHERE id=%s', (classroom_id,))
            conn.commit()
            print(f'[AI] DB updated rag_indexed=1 for classroom {classroom_id}')
    except Exception as db_err:
        print(f'[AI] DB update skipped (will update on next request): {db_err}')


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
        from langchain.schema import Document

        docs = []

        # Load PDFs (suppress float parsing noise)
        if pdf_files:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                loader = PyPDFDirectoryLoader(
                    str(lecture_dir), glob='**/*.pdf', silent_errors=True
                )
                docs.extend(loader.load())

        # Load TXT files
        for f in txt_files:
            try:
                docs.extend(TextLoader(str(f), encoding='utf-8').load())
            except Exception:
                try:
                    docs.extend(TextLoader(str(f), encoding='latin-1').load())
                except Exception as e:
                    print(f'[AI] Skipping {f.name}: {e}')

        # Load DOCX files
        for f in doc_files:
            try:
                docs.extend(Docx2txtLoader(str(f)).load())
            except Exception as e:
                print(f'[AI] Skipping {f.name}: {e}')

        if not docs:
            return {'ok': False, 'error': 'Files found but no text could be extracted (scanned images?)'}

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
            'message': (
                f'Trained on {len(docs)} pages '
                f'({len(chunks)} chunks) from {len(all_files)} file(s).'
            )
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def build_rag_index(classroom_id: int) -> dict:
    """
    PUBLIC API — called from Flask route.
    Starts background training and returns immediately.
    Returns {'ok': True, 'background': True, 'message': '...'}
    or {'ok': False, 'error': '...'} if already running or no files.
    """
    status = get_training_status(classroom_id)
    if status == 'running':
        return {'ok': False, 'error': 'Training is already in progress. Please wait.'}

    lecture_dir = _lecture_path(classroom_id)
    if not lecture_dir.exists() or not any(lecture_dir.iterdir()):
        return {'ok': False, 'error': 'No lecture files found. Upload PDFs first.'}

    # Reset status and spawn thread
    _training_status[classroom_id] = 'running'
    t = threading.Thread(
        target=_run_build_index,
        args=(classroom_id,),
        daemon=True,  # dies with the main process
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
        return True
    except Exception:
        return False


def rag_query(classroom_id: int, question: str, context_data: dict = None) -> str:
    llm = _get_llm_manager()

    extra_ctx = ''
    if context_data:
        if context_data.get('assignments'):
            extra_ctx += '\n\nASSIGNMENTS:\n'
            for a in context_data['assignments']:
                extra_ctx += f"- {a.get('title','?')} (due: {a.get('due_date','TBD')})\n"
        if context_data.get('class_name'):
            extra_ctx += f"\nClass: {context_data['class_name']}\n"

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
            print(f'[AI] RAG retrieval error: {e}')

    prompt = f"""You are a helpful AI teaching assistant for NeuroClass.
The course materials have not been indexed yet, so answer from general knowledge.
Be helpful, clear, and concise.
{extra_ctx}
STUDENT QUESTION: {question}

ANSWER:"""
    return llm.invoke(prompt)
