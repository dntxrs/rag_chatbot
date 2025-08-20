# ===================================================================================
# KODE FINAL STABIL - UNTUK DEPLOYMENT
# ===================================================================================

# 1. IMPORT PUSTAKA
import os
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
from dotenv import load_dotenv

nest_asyncio.apply()

# 2. KONFIGURASI DAN INISIALISASI
# Memuat environment variables dari file .env (untuk lokal) atau dari Railway
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

if not all([TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    raise ValueError("Satu atau lebih environment variable (API key) tidak ditemukan.")

genai.configure(api_key=GEMINI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Konfigurasi Model
multimodal_model = genai.GenerativeModel('gemini-2.5-flash')
generative_model = genai.GenerativeModel('gemini-2.5-pro')
embedding_model_name = 'models/text-embedding-004'

# Inisialisasi Text Splitter
text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=1500,
    chunk_overlap=300,
    length_function=len,
)

# 3. DEFINISI FUNGSI-FUNGSI INTI
# GANTI FUNGSI LAMA ANDA DENGAN VERSI STABIL INI


async def chunk_and_embed_content(update: Update, context: ContextTypes.DEFAULT_TYPE, content_list: list, file_name: str, user_id: str):
    """
    Fungsi generik untuk chunking, embedding, dan penyimpanan
    yang dioptimalkan untuk batch size besar dan stabilitas.
    """
    BATCH_SIZE = 128
    total_chunks = len(content_list)
    
    # Simpan ID pesan progres pertama kali
    sent_message = await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Memproses {total_chunks} potongan teks...")
    context.user_data['progress_message_id'] = sent_message.message_id
    
    # Kembali menggunakan loop sekuensial yang lebih stabil
    for i in range(0, total_chunks, BATCH_SIZE):
        batch_items = content_list[i:i + BATCH_SIZE]
        
        progress_msg = f"Memproses potongan {i+1}-{min(i+BATCH_SIZE, total_chunks)} dari {total_chunks}..."
        try:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=context.user_data['progress_message_id'], text=progress_msg)
        except Exception:
            pass # Abaikan jika pesan tidak berubah

        # --- PERBAIKAN UTAMA DI SINI (KATA KUNCI 'await' DIHAPUS) ---
        embedding_results = genai.embed_content(
            model=embedding_model_name,
            content=[item['content'] for item in batch_items],
            task_type="RETRIEVAL_DOCUMENT"
        )
        
        rows_to_insert = [{
            'content': item['content'],
            'page_number': item.get('page', 1),
            'embedding': embedding_results['embedding'][j],
            'file_name': file_name,
            'user_id': user_id
        } for j, item in enumerate(batch_items)]
        
        if rows_to_insert:
            supabase.table('documents').insert(rows_to_insert).execute()
        
        await asyncio.sleep(0.1)

    # Hapus pesan progres setelah semua selesai
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data['progress_message_id'])
    except Exception:
        pass
async def process_and_store_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, pdf_path: str, file_name: str, user_id: str, start_time: float):
    try:
        all_content_to_process = []
        doc = fitz.open(pdf_path)
        for i, page in enumerate(doc):
            if context.user_data.get('cancel_upload', False):
                await context.bot.send_message(chat_id=update.effective_chat.id, text=f"⚠️ Proses unggah dibatalkan.", parse_mode=ParseMode.HTML)
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
                        response = multimodal_model.generate_content(["Jelaskan gambar ini:", pil_image])
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

def find_relevant_chunks(question: str, user_id: str, focused_file: str = None) -> list:
    embedding_list = genai.embed_content(model=embedding_model_name, content=question, task_type="RETRIEVAL_QUERY")['embedding']
    params = {'query_embedding': embedding_list, 'user_id_input': user_id, 'match_threshold': 0.3, 'match_count': 5}
    function_name = 'match_documents'
    if focused_file:
        function_name = 'match_documents_by_file'
        params['file_name_input'] = focused_file
    response = supabase.rpc(function_name, params).execute()
    return response.data if response.data else []

def generate_answer(question: str, context_chunks: list, history: list) -> str:
    context_text = "\n\n".join([f"Kutipan dari file '{html.escape(chunk['file_name'])}' halaman {chunk['page_number']}:\n---\n{html.escape(chunk['content'])}\n---" for chunk in context_chunks])
    history_text = "\n".join([f"User: {html.escape(h['question'])}\nBot: {html.escape(h['answer'])}" for h in history])
    prompt = f"Anda adalah asisten AI. Jawab pertanyaan pengguna hanya berdasarkan KONTEKS DARI DOKUMEN. Jawab dalam bahasa yang sama dengan pertanyaan pengguna. Jika informasi tidak ada, katakan Anda tidak dapat menemukannya.\n\n--- KONTEKS DARI DOKUMEN ---\n{context_text}\n\n--- RIWAYAT PERCAKAPAN ---\n{history_text}\n\n--- PERTANYAAN PENGGUNA ---\n{question}\n\nJAWABAN ANDA:"
    response = generative_model.generate_content(prompt)
    return response.text if response.parts else "[RESPONS AI KOSONG]"

# 4. DEFINISI HANDLER TELEGRAM
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
    supabase.table('documents').delete().eq('user_id', user_id).eq('file_name', file_name).execute()
    file_path = f"{doc.file_id}_{file_name}" # Path relatif, bukan /content/
    new_file = await context.bot.get_file(doc.file_id)
    await new_file.download_to_drive(file_path)
    start_time = time.time()
    await update.message.reply_text(f"Memproses '<code>{html.escape(file_name)}</code>'...", parse_mode=ParseMode.HTML)
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
    except Exception as e: await update.message.reply_text(f"Gagal memproses file: {e}")
    finally:
        if file_extension != 'pdf' and os.path.exists(file_path):
            os.remove(file_path)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    question = update.message.text
    response = supabase.table('documents').select('id', count='exact').eq('user_id', user_id).execute()
    if response.count == 0:
        panduan_awal_text = "Halo! Sepertinya Anda belum mengunggah dokumen apa pun.\n\nSilakan <b>unggah file PDF, DOCX, atau TXT</b> terlebih dahulu."
        await update.message.reply_text(panduan_awal_text, parse_mode=ParseMode.HTML)
        return
    if 'history' not in context.user_data: context.user_data['history'] = []
    waiting_message = await update.message.reply_text('Menganalisis permintaan...')
    try:
        history_text = "\n".join([f"User: {h['question']}\nBot: {h['answer']}" for h in context.user_data.get('history', [])])
        refine_prompt = f"Tugas Anda adalah memperbaiki pertanyaan pengguna agar lebih optimal untuk pencarian di database.\n- Jika pertanyaan ambigu, perjelas menggunakan riwayat chat.\n- Jika pertanyaan sangat singkat (1-2 kata), ubah menjadi kalimat tanya. Contoh: 'akurasi' -> 'Berapa akurasi modelnya?'.\n- Jangan menambahkan sapaan atau jawaban.\n- Kembalikan HANYA teks pertanyaan yang sudah diperbaiki.\n\nRiwayat:\n{history_text}\n\nPertanyaan Asli: \"{question}\"\nPertanyaan Diperbaiki:"
        refined_question_response = generative_model.generate_content(refine_prompt)
        refined_question = refined_question_response.text.strip()
        await waiting_message.edit_text(f"Mencari informasi untuk: \"<i>{html.escape(refined_question)}</i>\"", parse_mode=ParseMode.HTML)
        focused_file = context.user_data.get('focused_document')
        relevant_chunks = find_relevant_chunks(refined_question, user_id, focused_file)
        if not relevant_chunks:
            await waiting_message.edit_text('Maaf, saya tidak dapat menemukan informasi spesifik mengenai itu di dokumen Anda.')
            return
        final_answer = generate_answer(refined_question, relevant_chunks, context.user_data['history']).strip()
        safe_answer = html.escape(final_answer)
        citations = "\n\n--- \n<b>Sumber Informasi:</b>\n"
        for chunk in relevant_chunks:
            safe_filename = html.escape(chunk['file_name'])
            safe_snippet = html.escape(chunk['content'][:80].replace("\n", " "))
            page_number = chunk['page_number']
            similarity = chunk['similarity'] * 100
            citations += f"• <code>{safe_filename}</code>, Hal. {page_number} (<b>Kemiripan: {similarity:.2f}%</b>): \"<i>{safe_snippet}...</i>\"\n"
        full_response = safe_answer + citations
        context.user_data['history'].append({'question': refined_question, 'answer': final_answer})
        context.user_data['history'] = context.user_data['history'][-5:]
        await waiting_message.delete()
        await update.message.reply_text(full_response, parse_mode=ParseMode.HTML)
    except Exception as e:
        try: await waiting_message.delete()
        except: pass
        await update.message.reply_text(f"Terjadi kesalahan: {html.escape(str(e))}")

# Handler utilitas lainnya
async def export_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    chat_history = context.user_data.get('history', [])

    if not chat_history:
        await update.message.reply_text("Riwayat percakapan masih kosong.")
        return

    await update.message.reply_text("Mempersiapkan file PDF...")
    try:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=12) # Menggunakan font standar yang aman
        
        # --- PERBAIKAN DI SINI ---
        # Menggunakan parameter 'text' dan 'new_x', 'new_y'
        pdf.cell(0, 10, text="Riwayat Percakapan Chatbot", 
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='C')

        for item in chat_history:
            # Batasi panjang teks untuk mencegah error
            question = item['question']
            answer = item['answer']
            
            safe_question = (question[:1500] + '...') if len(question) > 1500 else question
            safe_answer = (answer[:1500] + '...') if len(answer) > 1500 else answer

            # Menggunakan parameter 'text'
            pdf.set_font(style='B')
            pdf.multi_cell(0, 7, text=f"Anda: {safe_question.encode('latin-1', 'replace').decode('latin-1')}")
            pdf.set_font(style='')
            pdf.multi_cell(0, 7, text=f"Bot: {safe_answer.encode('latin-1', 'replace').decode('latin-1')}")
            pdf.ln(5)

        file_path = f"history_{user_id}.pdf"
        pdf.output(file_path)
        
        with open(file_path, 'rb') as f:
            await context.bot.send_document(chat_id=update.effective_chat.id, document=f)
        os.remove(file_path)

    except Exception as e:
        await update.message.reply_text(f"Gagal membuat PDF: {e}")

async def list_docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    try:
        response = supabase.table('documents').select('file_name', count='exact').eq('user_id', user_id).execute()
        if response.count == 0:
            await update.message.reply_text("Anda belum mengunggah dokumen.")
            return
        unique_files = sorted(list(set(d['file_name'] for d in response.data)))
        message = "<b>Dokumen tersimpan:</b>\n" + "\n".join(f"• <code>{html.escape(name)}</code>" for name in unique_files)
        await update.message.reply_text(message, parse_mode=ParseMode.HTML)
    except Exception as e: await update.message.reply_text(f"Error: {e}")

# GANTI FUNGSI LAMA ANDA DENGAN VERSI YANG SUDAH DIPERBAIKI INI

async def delete_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    try:
        # --- PERBAIKAN 1: Cara baru mengambil nama file ---
        # Mengambil semua teks setelah '/delete_doc '
        command = "/delete_doc "
        full_text = update.message.text
        # Pastikan pesan dimulai dengan perintah, lalu ambil sisanya
        if full_text.startswith(command):
            file_name_to_delete = full_text[len(command):].strip()
        else:
            # Fallback jika ada masalah, meskipun seharusnya tidak terjadi
            file_name_to_delete = " ".join(context.args)

        if not file_name_to_delete:
            await update.message.reply_text("Gunakan: /delete_doc <code>nama_file_lengkap.pdf</code>", parse_mode=ParseMode.HTML)
            return

        await update.message.reply_text(f"Mencari dan menghapus '<code>{html.escape(file_name_to_delete)}</code>'...", parse_mode=ParseMode.HTML)
        
        # --- PERBAIKAN 2: Cek hasil penghapusan ---
        # Menambahkan count='exact' untuk mendapatkan jumlah baris yang dihapus
        response = supabase.table('documents').delete(count='exact').eq('user_id', user_id).eq('file_name', file_name_to_delete).execute()

        # Periksa apakah ada baris yang benar-benar terhapus
        if response.count > 0:
            await update.message.reply_text(f"✅ Dokumen '<code>{html.escape(file_name_to_delete)}</code>' berhasil dihapus.", parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(f"⚠️ Dokumen '<code>{html.escape(file_name_to_delete)}</code>' tidak ditemukan.", parse_mode=ParseMode.HTML)

    except Exception as e:
        await update.message.reply_text(f"Terjadi kesalahan: {e}")

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('history', None)
    await update.message.reply_text('Riwayat percakapan sesi ini telah dihapus.')

async def cancel_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('processing_task') and not context.user_data['processing_task'].done():
        context.user_data['cancel_upload'] = True
        await update.message.reply_text("Sinyal pembatalan terkirim...")
    else:
        await update.message.reply_text("Tidak ada proses unggah yang sedang berjalan.")

# 5. FUNGSI UTAMA UNTUK MENJALANKAN BOT
# 5. FUNGSI UTAMA UNTUK MENJALANKAN BOT
def main():
    print("Bot sedang disiapkan...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list_docs", list_docs))
    application.add_handler(CommandHandler("delete_doc", delete_doc))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(CommandHandler("cancel", cancel_upload))
    application.add_handler(CommandHandler("export", export_chat))
    application.add_handler(CommandHandler("fokus", set_focus))
    application.add_handler(CommandHandler("hapus_fokus", remove_focus))
    application.add_handler(CommandHandler("summarize", summarize_document)) 
    
    application.add_handler(MessageHandler(
        filters.Document.PDF | filters.Document.TXT | filters.Document.DOCX, 
        handle_document
    ))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    

    
    print("Bot siap menerima pesan!")
    application.run_polling()

if __name__ == '__main__':
    main()
