# -*- coding: utf-8 -*-


# ===================================================================================
# KODE FINAL LENGKAP - PERBAIKAN FORMAT & FITUR FOKUS
# Termasuk: Dukungan .docx/.txt, Chunking Cerdas, Export PDF, Query Transformation,
#           Manajemen Dokumen, dan Fitur Fokus.
# TIDAK TERMASUK: Fitur Feedback (Tombol 👍/👎)
# ===================================================================================

# 0. INSTALASI PUSTAKA (Jalankan di sel pertama jika belum ada)
# !pip install python-telegram-bot==21.0.1 google-generativeai supabase PyMuPDF Pillow nest-asyncio langchain python-docx fpdf2

# 1. IMPORT PUSTAKA
import os
from dotenv import load_dotenv
import asyncio
import io
import html
import fitz  # PyMuPDF
import docx  # python-docx
import time
from PIL import Image
from fpdf import FPDF
import google.generativeai as genai
from supabase import create_client, Client
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import nest_asyncio
from langchain.text_splitter import RecursiveCharacterTextSplitter
import re

def escape_markdown_v2(text: str) -> str:
    """Fungsi untuk "melindungi" karakter khusus di MarkdownV2."""
    escape_chars = r'\_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)
nest_asyncio.apply()

# 2. KONFIGURASI DAN INISIALISASI
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

if not all([TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    raise ValueError("Satu atau lebih secret tidak ditemukan di Colab Secrets.")

genai.configure(api_key=GEMINI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Konfigurasi Model
multimodal_model = genai.GenerativeModel('gemini-2.5-flash')
generative_model = genai.GenerativeModel('gemini-2.5-pro')
embedding_model_name = 'models/text-embedding-004'

# Inisialisasi Text Splitter Cerdas (Chunking)
text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=1500,
    chunk_overlap=300,
    length_function=len,
)

# 3. DEFINISI FUNGSI-FUNGSI INTI

def find_relevant_chunks(question: str, user_id: str, focused_file: str = None) -> list:
    """Mencari potongan relevan, dengan opsi filter berdasarkan nama file."""
    embedding_list = genai.embed_content(model=embedding_model_name, content=question, task_type="RETRIEVAL_QUERY")['embedding']

    # Siapkan parameter dasar
    params = {
        'query_embedding': embedding_list,
        'user_id_input': user_id,
        'match_threshold': 0.5,
        'match_count': 5
    }

    # Nama fungsi RPC di Supabase
    function_name = 'match_documents'

    # Jika ada fokus file, gunakan fungsi RPC yang berbeda atau tambahkan parameter
    # (Asumsi Anda telah membuat fungsi RPC kedua bernama 'match_documents_by_file')
    if focused_file:
        function_name = 'match_documents_by_file'
        params['file_name_input'] = focused_file

    response = supabase.rpc(function_name, params).execute()
    return response.data if response.data else []

# CATATAN: Pastikan Anda membuat fungsi SQL kedua di Supabase untuk fitur fokus:
# CREATE OR REPLACE FUNCTION match_documents_by_file(query_embedding vector(768), user_id_input text, file_name_input text, match_threshold float, match_count int)
# RETURNS TABLE (...) AS $$
# BEGIN
#   RETURN QUERY
#   SELECT ... FROM documents
#   WHERE documents.user_id = user_id_input AND documents.file_name = file_name_input AND 1 - (documents.embedding <=> query_embedding) > match_threshold
#   ORDER BY documents.embedding <=> query_embedding
#   LIMIT match_count;
# END;
# $$ LANGUAGE plpgsql;


async def chunk_and_embed_content(update: Update, context: ContextTypes.DEFAULT_TYPE, content_list: list, file_name: str, user_id: str):
    """Fungsi generik untuk chunking, embedding, dan penyimpanan."""
    BATCH_SIZE, total_chunks = 32, len(content_list)
    for i in range(0, total_chunks, BATCH_SIZE):
        batch_items = content_list[i:i+BATCH_SIZE]
        progress_msg = f"Memproses potongan {i+1}-{min(i+BATCH_SIZE, total_chunks)} dari {total_chunks} untuk '{file_name}'..."
        await context.bot.send_message(chat_id=update.effective_chat.id, text=progress_msg)
        embedding_results = genai.embed_content(model=embedding_model_name, content=[item['content'] for item in batch_items], task_type="RETRIEVAL_DOCUMENT")
        rows_to_insert = [{'content': item['content'], 'page_number': item.get('page', 1), 'embedding': embedding_results['embedding'][j], 'file_name': file_name, 'user_id': user_id} for j, item in enumerate(batch_items)]
        if rows_to_insert:
            supabase.table('documents').insert(rows_to_insert).execute()
        await asyncio.sleep(0.1)

async def process_and_store_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, pdf_path: str, file_name: str, user_id: str, start_time: float):
    """Fungsi spesifik untuk memproses file PDF (teks dan gambar)."""
    try:
        all_content_to_process = []
        doc = fitz.open(pdf_path)
        for i, page in enumerate(doc):
            if context.user_data.get('cancel_upload', False):
                await context.bot.send_message(chat_id=update.effective_chat.id, text=f"⚠️ Proses unggah untuk '<code>{html.escape(file_name)}</code>' dibatalkan.", parse_mode=ParseMode.HTML)
                supabase.table('documents').delete().eq('user_id', user_id).eq('file_name', file_name).execute()
                return
            page_number = i + 1
            text = page.get_text("text")
            if text:
                chunks = text_splitter.split_text(text)
                for chunk in chunks: all_content_to_process.append({'content': chunk, 'page': page_number})
            image_list = page.get_images(full=True)
            if image_list:
                for img_info in image_list:
                    try:
                        base_image = doc.extract_image(img_info[0])
                        pil_image = Image.open(io.BytesIO(base_image["image"]))
                        response = multimodal_model.generate_content(["Jelaskan gambar ini secara detail:", pil_image])
                        img_chunks = text_splitter.split_text(f"[Deskripsi Gambar: {response.text.strip()}]")
                        for chunk in img_chunks: all_content_to_process.append({'content': chunk, 'page': page_number})
                    except Exception as e: print(f"Gagal deskripsi gambar di hal {page_number}: {e}")
        if not all_content_to_process:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="Dokumen PDF tidak berisi konten yang bisa diproses.")
            return
        await chunk_and_embed_content(update, context, all_content_to_process, file_name, user_id)
        duration = time.time() - start_time
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ Dokumen '<code>{html.escape(file_name)}</code>' berhasil diproses dalam {duration:.2f} detik.", parse_mode=ParseMode.HTML)
    except Exception as e: await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Terjadi kesalahan saat memproses PDF: {e}")
    finally:
        if 'processing_task' in context.user_data: del context.user_data['processing_task']
        if 'cancel_upload' in context.user_data: del context.user_data['cancel_upload']
        if os.path.exists(pdf_path): os.remove(pdf_path)

def generate_answer(question: str, context_chunks: list, history: list) -> str:
    """Menghasilkan jawaban berdasarkan konteks dan riwayat."""
    context_text = "\n\n".join([f"Kutipan dari file '{html.escape(chunk['file_name'])}' halaman {chunk['page_number']}:\n---\n{html.escape(chunk['content'])}\n---" for chunk in context_chunks])
    history_text = "\n".join([f"User: {html.escape(h['question'])}\nBot: {html.escape(h['answer'])}" for h in history])
    prompt = f"Anda adalah asisten AI. Jawab pertanyaan pengguna hanya berdasarkan KONTEKS DARI DOKUMEN. Jawab dalam bahasa yang sama dengan pertanyaan pengguna. Jika informasi tidak ada, katakan Anda tidak dapat menemukannya.\n\n--- KONTEKS DARI DOKUMEN ---\n{context_text}\n\n--- RIWAYAT PERCAKAPAN ---\n{history_text}\n\n--- PERTANYAAN PENGGUNA ---\n{question}\n\nJAWABAN ANDA:"
    response = generative_model.generate_content(prompt)
    return response.text if response.parts else "[RESPONS AI KOSONG / DI BLOKIR]"

# 4. DEFINISI FUNGSI-FUNGSI HANDLER TELEGRAM
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    help_text = ("<b>Halo! Saya asisten dokumen pribadi Anda.</b>\n\n"
                 "<b>Perintah yang tersedia:</b>\n"
                 "• Kirim file PDF, DOCX, atau TXT untuk dianalisis.\n"
                 "• /fokus <code>nama_file.pdf</code> - Fokus tanya jawab ke satu file.\n"
                 "• /hapus_fokus - Kembali bertanya ke semua file.\n"
                 "• /list_docs - Lihat daftar dokumen.\n"
                 "• /delete_doc <code>nama_file.pdf</code> - Hapus dokumen.\n"
                 "• /export - Ekspor riwayat chat ke PDF.\n"
                 "• /clear - Hapus riwayat chat sesi ini.\n"
                 "• /cancel - Batalkan proses upload file.")
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

async def set_focus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_name = " ".join(context.args)
    if not file_name:
        await update.message.reply_text("Gunakan: /fokus <code>nama_file.pdf</code>", parse_mode=ParseMode.HTML)
        return
    context.user_data['focused_document'] = file_name
    await update.message.reply_text(f"✅ Fokus sekarang diatur ke: <code>{html.escape(file_name)}</code>", parse_mode=ParseMode.HTML)

async def remove_focus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'focused_document' in context.user_data:
        del context.user_data['focused_document']
        await update.message.reply_text("✅ Fokus telah dihapus. Anda sekarang bisa bertanya dari semua dokumen.")
    else:
        await update.message.reply_text("Tidak ada fokus yang sedang aktif.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    doc = update.message.document
    file_name = doc.file_name
    
    if context.user_data.get('processing_task') and not context.user_data['processing_task'].done():
        await update.message.reply_text("Harap tunggu, proses lain sedang berjalan.")
        return

    # Hapus data lama dengan nama file yang sama
    supabase.table('documents').delete().eq('user_id', user_id).eq('file_name', file_name).execute()

    # ===== PERBAIKAN UTAMA DI SINI =====
    # Menghapus /content/ agar file disimpan di direktori kerja saat ini
    file_path = f"{doc.file_id}_{file_name}"
    # ====================================
    
    new_file = await context.bot.get_file(doc.file_id)
    await new_file.download_to_drive(file_path)
    
    start_time = time.time()
    await update.message.reply_text(f"Memproses '<code>{html.escape(file_name)}</code>'. Gunakan /cancel untuk membatalkan jika proses lama.", parse_mode=ParseMode.HTML)
    
    file_extension = ""
    try:
        file_extension = file_name.split('.')[-1].lower()
        if file_extension == 'pdf':
            context.user_data['cancel_upload'] = False
            task = asyncio.create_task(process_and_store_pdf(update, context, file_path, file_name, user_id, start_time))
            context.user_data['processing_task'] = task
            return

        full_text = ""
        if file_extension == 'docx':
            document = docx.Document(file_path)
            full_text = "\n".join([para.text for para in document.paragraphs])
        elif file_extension == 'txt':
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                full_text = f.read()

        if not full_text.strip():
            await update.message.reply_text("Dokumen ini tidak berisi teks yang bisa diproses.")
            return

        chunks = text_splitter.split_text(full_text)
        content_to_process = [{'content': chunk} for chunk in chunks]
        await chunk_and_embed_content(update, context, content_to_process, file_name, user_id)
        duration = time.time() - start_time
        await update.message.reply_text(f"✅ Dokumen '<code>{html.escape(file_name)}</code>' berhasil diproses dalam {duration:.2f} detik.", parse_mode=ParseMode.HTML)

    except Exception as e:
        await update.message.reply_text(f"Gagal memproses file: {e}")
    finally:
        if file_extension != 'pdf' and os.path.exists(file_path):
            os.remove(file_path)

# GANTI FUNGSI HANDLE_MESSAGE ANDA DENGAN YANG INI

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    question = update.message.text

    response = supabase.table('documents').select('id').eq('user_id', user_id).limit(1).execute()
    if not response.data:
        panduan_awal_text = "Halo! Sepertinya Anda belum mengunggah dokumen apa pun.\n\nSilakan **unggah file PDF, DOCX, atau TXT** terlebih dahulu agar saya bisa menjawab pertanyaan Anda."
        await update.message.reply_text(panduan_awal_text, parse_mode=ParseMode.HTML)
        return
        
    if 'history' not in context.user_data: context.user_data['history'] = []
    
    waiting_message = await update.message.reply_text('Memahami pertanyaan Anda...')
    try:
        history_text = "\n".join([f"User: {h['question']}\nBot: {h['answer']}" for h in context.user_data.get('history', [])])
        refine_prompt = f"Riwayat percakapan:\n{history_text}\n\nPertanyaan pengguna: \"{question}\"\nTugas Anda:\n1. Analisis pertanyaan pengguna. Jika ambigu, tulis ulang menjadi versi yang lebih jelas.\n2. Jika hanya satu atau dua kata, ubah menjadi pertanyaan lengkap. Contoh: 'RAG' -> 'Jelaskan tentang RAG'.\n3. Jika sudah jelas, kembalikan apa adanya.\nHanya kembalikan teks pertanyaan final."
        refined_question_response = generative_model.generate_content(refine_prompt)
        refined_question = refined_question_response.text.strip()
        
        await waiting_message.edit_text(f"Mencari informasi untuk: '{refined_question}'...")
        
        focused_file = context.user_data.get('focused_document')
        relevant_chunks = find_relevant_chunks(refined_question, user_id, focused_file)
        
        if not relevant_chunks:
            await waiting_message.edit_text('Maaf, saya tidak dapat menemukan informasi spesifik mengenai itu di dokumen Anda. Silakan coba pertanyaan lain.')
            return

        final_answer = generate_answer(refined_question, relevant_chunks, context.user_data['history']).strip()
        
        safe_answer = escape_markdown_v2(final_answer)

        citations = f"\n\n\\-\\-\\-\n*Sumber Informasi:*\n"

        for chunk in relevant_chunks:
            safe_filename = escape_markdown_v2(chunk['file_name'])
            safe_snippet = escape_markdown_v2(chunk['content'][:80].replace("\n", " "))
            page_number = chunk['page_number']
            similarity = chunk['similarity'] * 100
            
            # ===== PERBAIKAN UTAMA DI SINI =====
            citations += f"• `{safe_filename}`, Hal\\. {page_number} \\(*Kemiripan: {similarity:.2f}%*\\): \"_{safe_snippet}..._\"\n"
            # ====================================
        
        full_response = safe_answer + citations

        context.user_data['history'].append({'question': refined_question, 'answer': final_answer})
        context.user_data['history'] = context.user_data['history'][-5:]
        
        await waiting_message.delete()
        
        await update.message.reply_text(full_response, parse_mode=ParseMode.MARKDOWN_V2)

    except Exception as e:
        try:
            await waiting_message.delete()
        except Exception as delete_err:
            print(f"Gagal menghapus pesan tunggu: {delete_err}")
        
        error_message = f"Terjadi kesalahan: {str(e)}"
        await update.message.reply_text(escape_markdown_v2(error_message), parse_mode=ParseMode.MARKDOWN_V2)

async def export_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    chat_history = context.user_data.get('history', [])
    if not chat_history:
        await update.message.reply_text("Riwayat percakapan masih kosong.")
        return
    await update.message.reply_text("Mempersiapkan file PDF riwayat percakapan...")
    try:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Arial", size=12)
        pdf.cell(0, 10, txt="Riwayat Percakapan Chatbot", ln=True, align='C')
        pdf.ln(10)
        for item in chat_history:
            pdf.set_font("Arial", 'B', 12)
            pdf.multi_cell(0, 7, f"Anda: {item['question'].encode('latin-1', 'replace').decode('latin-1')}")
            pdf.set_font("Arial", '', 12)
            pdf.multi_cell(0, 7, f"Bot: {item['answer'].encode('latin-1', 'replace').decode('latin-1')}")
            pdf.ln(5)
        file_path = f"/content/history_{user_id}.pdf"
        pdf.output(file_path)
        with open(file_path, 'rb') as f:
            await context.bot.send_document(chat_id=update.effective_chat.id, document=f)
        os.remove(file_path)
    except Exception as e: await update.message.reply_text(f"Gagal membuat PDF: {e}")

async def list_docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    try:
        response = supabase.table('documents').select('file_name').eq('user_id', user_id).execute()
        if not response.data:
            await update.message.reply_text("Anda belum mengunggah dokumen.")
            return
        unique_files = sorted(list(set(d['file_name'] for d in response.data)))
        message = "<b>Dokumen tersimpan:</b>\n" + "\n".join(f"• <code>{html.escape(name)}</code>" for name in unique_files)
        await update.message.reply_text(message, parse_mode=ParseMode.HTML)
    except Exception as e: await update.message.reply_text(f"Error: {e}")

async def delete_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    try:
        file_name = " ".join(context.args)
        if not file_name:
            await update.message.reply_text("Gunakan: /delete_doc <code>nama_file.pdf</code>", parse_mode=ParseMode.HTML)
            return
        supabase.table('documents').delete().eq('user_id', user_id).eq('file_name', file_name).execute()
        await update.message.reply_text(f"Dokumen '<code>{html.escape(file_name)}</code>' telah dihapus.", parse_mode=ParseMode.HTML)
    except Exception as e: await update.message.reply_text(f"Error: {e}")

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('history', None)
    await update.message.reply_text('Riwayat percakapan sesi ini telah dihapus.')

async def cancel_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('processing_task') and not context.user_data['processing_task'].done():
        context.user_data['cancel_upload'] = True
        await update.message.reply_text("Sinyal pembatalan terkirim...")
    else:
        await update.message.reply_text("Tidak ada proses unggah yang sedang berjalan.")


def main():
    print("Bot versi final (dengan fitur fokus) sedang disiapkan...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list_docs", list_docs))
    application.add_handler(CommandHandler("delete_doc", delete_doc))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(CommandHandler("cancel", cancel_upload))
    application.add_handler(CommandHandler("export", export_chat))
    application.add_handler(CommandHandler("fokus", set_focus))
    application.add_handler(CommandHandler("hapus_fokus", remove_focus))

    application.add_handler(MessageHandler(filters.Document.PDF | filters.Document.TXT | filters.Document.DOCX, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot siap menerima pesan!")
    application.run_polling()

if __name__ == '__main__':
    main()
