import os
import json
import logging
import requests
import threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import openai
import io

# ---------------------------------------------------------------
# SECCIÓN: CARGA E INICIALIZACIÓN
# ---------------------------------------------------------------

# Cargar variables de entorno
load_dotenv()

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Inicializar Flask
app = Flask(__name__)

# Variables de entorno
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("META_TOKEN")
PHONE_NUMBER_ID = os.getenv("WABA_PHONE_ID")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
SHEETS_ID_LEADS = os.getenv("SHEETS_ID_LEADS")
SHEETS_TITLE_LEADS = os.getenv("SHEETS_TITLE_LEADS", "Prospectos SECOM Auto")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

# Inicializar clientes de Google
google_creds = None
sheets_client = None
drive_service = None
openai_client = None

try:
    if GOOGLE_CREDENTIALS_JSON:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        google_creds = Credentials.from_service_account_info(creds_dict)
        sheets_client = gspread.authorize(google_creds)
        drive_service = build('drive', 'v3', credentials=google_creds)
        logger.info("✅ Clientes de Google inicializados correctamente")
except Exception as e:
    logger.error(f"❌ Error inicializando clientes de Google: {e}")

try:
    if OPENAI_API_KEY:
        openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
        logger.info("✅ Cliente OpenAI inicializado correctamente")
except Exception as e:
    logger.error(f"❌ Error inicializando cliente OpenAI: {e}")

# Controles en memoria
PROCESSED_MESSAGE_IDS = {}
GREETED_USERS = {}
LAST_INTENT = {}
USER_CONTEXT = {}

# ---------------------------------------------------------------
# SECCIÓN: GOOGLE SHEETS (SECOM)
# ---------------------------------------------------------------

def find_client_in_sheet(phone):
    """Busca cliente en Google Sheets por número de teléfono"""
    if not sheets_client or not SHEETS_ID_LEADS:
        return None
    
    try:
        sheet = sheets_client.open_by_key(SHEETS_ID_LEADS)
        worksheet = sheet.worksheet(SHEETS_TITLE_LEADS)
        records = worksheet.get_all_records()
        
        # Normalizar phone (últimos 10 dígitos)
        phone_normalized = phone[-10:] if len(phone) >= 10 else phone
        
        for record in records:
            record_phone = str(record.get('WhatsApp', '') or record.get('Teléfono', '') or '')
            if record_phone and record_phone[-10:] == phone_normalized:
                return {
                    'nombre': record.get('Nombre', ''),
                    'rfc': record.get('RFC', ''),
                    'email': record.get('Email', ''),
                    'vencimiento_poliza': record.get('Vencimiento Póliza', ''),
                    'estatus': record.get('Estatus', '')
                }
    except Exception as e:
        logger.error(f"❌ Error buscando cliente en sheet: {e}")
    
    return None

def update_client_status(phone, status, additional_data=None):
    """Actualiza estatus del cliente en Google Sheets"""
    if not sheets_client or not SHEETS_ID_LEADS:
        return False
    
    try:
        sheet = sheets_client.open_by_key(SHEETS_ID_LEADS)
        worksheet = sheet.worksheet(SHEETS_TITLE_LEADS)
        records = worksheet.get_all_records()
        
        phone_normalized = phone[-10:] if len(phone) >= 10 else phone
        
        for i, record in enumerate(records, start=2):  # start=2 porque la primera fila es encabezado
            record_phone = str(record.get('WhatsApp', '') or record.get('Teléfono', '') or '')
            if record_phone and record_phone[-10:] == phone_normalized:
                # Actualizar estatus
                worksheet.update_cell(i, worksheet.find("Estatus").col, status)
                
                # Actualizar fecha de último contacto
                if worksheet.find("Último Contacto"):
                    worksheet.update_cell(i, worksheet.find("Último Contacto").col, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                
                # Actualizar datos adicionales
                if additional_data:
                    for key, value in additional_data.items():
                        if worksheet.find(key):
                            worksheet.update_cell(i, worksheet.find(key).col, value)
                
                logger.info(f"✅ Estatus actualizado para {phone}: {status}")
                return True
    except Exception as e:
        logger.error(f"❌ Error actualizando estatus del cliente: {e}")
    
    return False

def register_new_interaction(phone, interaction_type, details):
    """Registra nueva interacción en Google Sheets"""
    if not sheets_client or not SHEETS_ID_LEADS:
        return False
    
    try:
        client_data = find_client_in_sheet(phone)
        if not client_data:
            # Crear nuevo registro si no existe
            sheet = sheets_client.open_by_key(SHEETS_ID_LEADS)
            worksheet = sheet.worksheet(SHEETS_TITLE_LEADS)
            
            new_row = [
                phone[-10:],  # WhatsApp (últimos 10 dígitos)
                "",  # Nombre (desconocido)
                "",  # Email
                "",  # RFC
                "Nuevo Prospecto",  # Estatus
                interaction_type,  # Tipo de Interacción
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # Último Contacto
                details,  # Detalles
                "",  # Vencimiento Póliza
                datetime.now().strftime("%Y-%m-%d")  # Fecha Registro
            ]
            
            worksheet.append_row(new_row)
            logger.info(f"✅ Nuevo cliente registrado: {phone}")
        else:
            # Actualizar registro existente
            update_client_status(phone, interaction_type, {
                "Tipo de Interacción": interaction_type,
                "Detalles": details,
                "Último Contacto": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
        
        return True
    except Exception as e:
        logger.error(f"❌ Error registrando interacción: {e}")
        return False

# ---------------------------------------------------------------
# SECCIÓN: GOOGLE DRIVE (RESPALDO)
# ---------------------------------------------------------------

def save_to_drive(file_bytes, filename, client_phone, mime_type=None):
    """Guarda archivo en Google Drive"""
    if not drive_service or not DRIVE_FOLDER_ID:
        logger.warning("❌ Servicio de Drive no disponible")
        return None
    
    try:
        # Determinar tipo MIME
        if not mime_type:
            if filename.lower().endswith(('.jpg', '.jpeg', '.png')):
                mime_type = 'image/jpeg'
            elif filename.lower().endswith('.pdf'):
                mime_type = 'application/pdf'
            elif filename.lower().endswith(('.mp3', '.ogg', '.wav')):
                mime_type = 'audio/mpeg'
            else:
                mime_type = 'application/octet-stream'
        
        # Crear nombre de carpeta del cliente
        client_data = find_client_in_sheet(client_phone)
        client_name = client_data.get('nombre', '') if client_data else ''
        folder_name = f"{client_name}_{client_phone[-4:]}" if client_name else f"Cliente_{client_phone[-4:]}"
        
        # Buscar o crear carpeta del cliente
        folder_query = f"name='{folder_name}' and '{DRIVE_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        folder_results = drive_service.files().list(q=folder_query).execute()
        
        if folder_results.get('files'):
            folder_id = folder_results['files'][0]['id']
        else:
            # Crear nueva carpeta
            folder_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [DRIVE_FOLDER_ID]
            }
            folder = drive_service.files().create(body=folder_metadata, fields='id').execute()
            folder_id = folder.get('id')
        
        # Subir archivo
        file_metadata = {
            'name': filename,
            'parents': [folder_id]
        }
        
        file_stream = io.BytesIO(file_bytes)
        media = MediaIoBaseUpload(file_stream, mimetype=mime_type, resumable=True)
        
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink'
        ).execute()
        
        logger.info(f"✅ Archivo guardado en Drive: {filename}")
        return file.get('webViewLink')
    
    except Exception as e:
        logger.error(f"❌ Error guardando archivo en Drive: {e}")
        return None

# ---------------------------------------------------------------
# SECCIÓN: WHATSAPP MESSAGING
# ---------------------------------------------------------------

def send_message_async(to, text):
    """Envía mensaje de forma asíncrona"""
    def _send():
        try:
            send_message(to, text)
        except Exception as e:
            logger.error(f"❌ Error enviando mensaje asíncrono: {e}")
    
    thread = threading.Thread(target=_send)
    thread.daemon = True
    thread.start()

def send_message(to, text):
    """Envía mensaje de texto por WhatsApp"""
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info(f"✅ Mensaje enviado a {to}")
        else:
            logger.error(f"❌ Error enviando mensaje: {response.status_code} - {response.text}")
        return response
    except Exception as e:
        logger.error(f"❌ Error enviando mensaje: {e}")
        return None

def send_template(template_name, to, variables=None):
    """Envía plantilla de WhatsApp"""
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    
    components = []
    if variables:
        components = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(var)} for var in variables]
        }]
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "es_MX"},
            "components": components
        }
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info(f"✅ Plantilla {template_name} enviada a {to}")
        else:
            logger.error(f"❌ Error enviando plantilla: {response.status_code} - {response.text}")
        return response
    except Exception as e:
        logger.error(f"❌ Error enviando plantilla: {e}")
        return None

def send_main_menu(to, client_name=None):
    """Envía el menú principal personalizado"""
    greeting = f"👋 Hola {client_name}, " if client_name else "👋 Hola, "
    
    menu_text = (
        f"{greeting}soy Vicky, tu asistente virtual de SECOM. "
        "Estoy aquí para ayudarte con tus seguros y pólizas.\n\n"
        "👉 *Elige una opción:*\n\n"
        "1️⃣ *Renovar Póliza* - Renovación y seguimiento\n"
        "2️⃣ *Documentos Seguro Auto* - Envío de documentos\n"
        "3️⃣ *Promociones y Descuentos* - Ofertas especiales\n"
        "4️⃣ *Seguimiento* - Consulta el estatus de tu trámite\n"
        "5️⃣ *Préstamos IMSS* - Financiamiento para pensionados\n"
        "6️⃣ *VRIM* - Tarjetas médicas y beneficios\n"
        "7️⃣ *Contactar con Christian* - Atención personalizada\n\n"
        "Escribe el número de la opción o 'menu' para volver a ver este menú."
    )
    
    send_message_async(to, menu_text)

# ---------------------------------------------------------------
# SECCIÓN: MOTOR GPT
# ---------------------------------------------------------------

def ask_gpt(prompt, system_message=None):
    """Consulta a GPT para respuestas naturales"""
    if not openai_client:
        return None
    
    try:
        system_msg = system_message or (
            "Eres Vicky, asistente virtual de Christian López en SECOM. "
            "Eres cálida, profesional y servicial. Responde en español de manera "
            "breve, clara y orientada a soluciones. Usa emojis moderadamente. "
            "Si no tienes información específica, sugiere contactar al asesor."
        )
        
        response = openai_client.chat.completions.create(
            model="gpt-4-turbo-preview",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt}
            ],
            max_tokens=150,
            temperature=0.7
        )
        
        return response.choices[0].message.content.strip()
    
    except Exception as e:
        logger.error(f"❌ Error consultando GPT: {e}")
        return None

# ---------------------------------------------------------------
# SECCIÓN: FLUJOS SECOM PRINCIPALES
# ---------------------------------------------------------------

def handle_policy_renewal(to, client_phone, message_text):
    """Maneja el flujo de renovación de póliza"""
    client_data = find_client_in_sheet(client_phone)
    
    if "vencimiento" in message_text.lower() or "renovar" in message_text.lower():
        if client_data and client_data.get('vencimiento_poliza'):
            # Cliente existente con fecha de vencimiento
            vencimiento = client_data['vencimiento_poliza']
            response = (
                f"📅 Tu póliza vence el *{vencimiento}*. "
                f"Te contactaré un mes antes para gestionar la renovación. "
                "¿Hay algo más en lo que pueda ayudarte?"
            )
        else:
            # Nuevo cliente o sin fecha registrada
            response = (
                "🔄 Para programar la renovación de tu póliza, necesito saber:\n\n"
                "📅 *¿Cuándo vence tu póliza actual?* (formato: DD/MM/AAAA)\n\n"
                "Una vez que me compartas la fecha, programaré el recordatorio automático."
            )
            USER_CONTEXT[client_phone] = {"context": "awaiting_policy_date", "timestamp": datetime.now()}
        
        send_message_async(to, response)
        register_new_interaction(client_phone, "Renovación Póliza", f"Consulta: {message_text}")
        return True
    
    elif USER_CONTEXT.get(client_phone, {}).get("context") == "awaiting_policy_date":
        # Procesar fecha de vencimiento
        try:
            # Intentar parsear fecha
            date_str = message_text.strip()
            date_obj = datetime.strptime(date_str, "%d/%m/%Y")
            
            # Actualizar en Sheets
            update_client_status(client_phone, "Póliza Activa", {
                "Vencimiento Póliza": date_obj.strftime("%Y-%m-%d")
            })
            
            # Calcular fecha de recordatorio (1 mes antes)
            reminder_date = date_obj - timedelta(days=30)
            
            response = (
                f"✅ Perfecto! He registrado que tu póliza vence el *{date_str}*. "
                f"Te contactaré el *{reminder_date.strftime('%d/%m/%Y')}* "
                "para gestionar la renovación. ¡Gracias!"
            )
            
            # Programar recordatorio (en producción usaría Celery o similar)
            logger.info(f"📅 Recordatorio programado para {reminder_date}")
            
        except ValueError:
            response = "❌ Formato de fecha incorrecto. Por favor usa DD/MM/AAAA (ej: 25/12/2024)"
        
        # Limpiar contexto
        USER_CONTEXT.pop(client_phone, None)
        send_message_async(to, response)
        register_new_interaction(client_phone, "Fecha Vencimiento Registrada", f"Fecha: {message_text}")
        return True
    
    return False

def handle_auto_documents(to, client_phone, message_text):
    """Maneja el flujo de documentos para seguro auto"""
    response = (
        "🚗 *Documentos para Seguro Auto*\n\n"
        "Para cotizar o renovar tu seguro de auto, necesito:\n\n"
        "📷 *INE* (foto frontal y posterior)\n"
        "📄 *Tarjeta de Circulación* (foto de ambos lados)\n"
        "🔢 *Número de Placa* (si no tienes los documentos)\n\n"
        "Puedes enviar las fotos o documentos ahora mismo. "
        "Los guardaré de forma segura en tu expediente."
    )
    
    send_message_async(to, response)
    register_new_interaction(client_phone, "Solicitud Documentos Auto", "Cliente solicitó info documentos")
    USER_CONTEXT[client_phone] = {"context": "awaiting_auto_docs", "timestamp": datetime.now()}
    
    return True

def handle_promotions(to, client_phone):
    """Maneja el flujo de promociones"""
    response = (
        "🎁 *Promociones y Descuentos Vigentes*\n\n"
        "🌟 *Seguro Auto Plus*: 15% descuento en renovación\n"
        "🏥 *VRIM Familiar*: 2 meses gratis al contratar anual\n"
        "👵 *Pensionados IMSS*: Tasas preferenciales en préstamos\n"
        "🚗 *Auto Nuevo*: Cobertura ampliada sin costo extra\n\n"
        "¿Te interesa alguna de estas promociones? "
        "Escribe el número o 'más info' para detalles."
    )
    
    send_message_async(to, response)
    register_new_interaction(client_phone, "Consulta Promociones", "Cliente solicitó promociones")
    return True

def handle_follow_up(to, client_phone):
    """Maneja el flujo de seguimiento"""
    client_data = find_client_in_sheet(client_phone)
    
    if client_data:
        status = client_data.get('estatus', 'No especificado')
        response = (
            f"📊 *Seguimiento de tu Trámite*\n\n"
            f"📋 *Estatus actual:* {status}\n"
            f"👤 *Asesor asignado:* Christian López\n"
            f"📞 *Contacto:* {ADVISOR_NUMBER}\n\n"
            "¿Necesitas información específica sobre algún trámite?"
        )
    else:
        response = (
            "🔍 No encuentro tu información en el sistema. "
            "¿Podrías proporcionarme tu número de póliza o "
            "prefieres que te contacte Christian para ayudarte?"
        )
    
    send_message_async(to, response)
    register_new_interaction(client_phone, "Consulta Seguimiento", "Cliente solicitó seguimiento")
    return True

def handle_contact_advisor(to, client_phone, message_text):
    """Maneja el flujo de contacto con el asesor"""
    client_data = find_client_in_sheet(client_phone)
    client_name = client_data.get('nombre', 'Cliente') if client_data else 'Cliente'
    
    # Notificar al asesor
    advisor_message = (
        f"🔔 *Nueva Solicitud de Contacto - Vicky Bot*\n\n"
        f"👤 *Nombre:* {client_name}\n"
        f"📱 *WhatsApp:* {client_phone}\n"
        f"💬 *Mensaje:* \"{message_text}\"\n"
        f"⏰ *Hora:* {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )
    
    if ADVISOR_NUMBER:
        send_message_async(ADVISOR_NUMBER, advisor_message)
    
    # Confirmar al cliente
    client_response = (
        f"✅ Perfecto {client_name}, he notificado a *Christian López*.\n\n"
        "📞 Él se pondrá en contacto contigo en breve para brindarte "
        "atención personalizada.\n\n"
        "Mientras tanto, ¿hay algo más en lo que pueda asistirte?"
    )
    
    send_message_async(to, client_response)
    register_new_interaction(client_phone, "Solicitud Contacto Asesor", f"Mensaje: {message_text}")
    
    return True

# ---------------------------------------------------------------
# SECCIÓN: WEBHOOK PRINCIPAL
# ---------------------------------------------------------------

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Verificación del webhook"""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("✅ Webhook verificado correctamente")
        return challenge, 200
    else:
        logger.warning("❌ Fallo en la verificación del webhook")
        return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def receive_message():
    """Endpoint principal para recibir mensajes"""
    data = request.get_json()
    logger.info(f"📩 Mensaje recibido: {data}")

    if not data or "entry" not in data:
        return jsonify({"status": "ignored"}), 200

    # Limpieza periódica de controles en memoria
    now = datetime.now().timestamp()
    MSG_TTL = 600
    GREET_TTL = 24 * 3600
    CTX_TTL = 4 * 3600

    # Limpiar mensajes procesados antiguos
    if len(PROCESSED_MESSAGE_IDS) > 5000:
        PROCESSED_MESSAGE_IDS = {k: v for k, v in PROCESSED_MESSAGE_IDS.items() if now - v < MSG_TTL}
    
    # Limpiar usuarios saludados antiguos
    if len(GREETED_USERS) > 5000:
        GREETED_USERS = {k: v for k, v in GREETED_USERS.items() if now - v < GREET_TTL}
    
    # Limpiar contextos antiguos
    if len(USER_CONTEXT) > 5000:
        USER_CONTEXT = {k: v for k, v in USER_CONTEXT.items() if now - v.get("timestamp", now) < CTX_TTL}

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            
            # Ignorar actualizaciones de estado
            if "statuses" in value:
                continue
            
            messages = value.get("messages", [])
            if not messages:
                continue
            
            message = messages[0]
            msg_id = message.get("id")
            msg_type = message.get("type")
            sender = message.get("from")
            
            # Obtener nombre del perfil
            profile_name = None
            try:
                contacts = value.get("contacts", [])
                if contacts:
                    profile_name = contacts[0].get("profile", {}).get("name")
            except Exception:
                pass
            
            logger.info(f"🧾 id={msg_id} type={msg_type} from={sender} profile={profile_name}")
            
            # Verificar duplicados
            if msg_id:
                last_seen = PROCESSED_MESSAGE_IDS.get(msg_id)
                if last_seen and (now - last_seen) < MSG_TTL:
                    logger.info(f"🔁 Mensaje duplicado ignorado: {msg_id}")
                    continue
                PROCESSED_MESSAGE_IDS[msg_id] = now
            
            # Buscar información del cliente
            client_data = find_client_in_sheet(sender)
            client_name = client_data.get('nombre') if client_data else None
            
            # Manejar diferentes tipos de mensaje
            if msg_type == "text":
                handle_text_message(sender, message, client_name, client_data)
            
            elif msg_type in ["image", "document", "audio"]:
                handle_media_message(sender, message, msg_type, client_name)
            
            else:
                logger.info(f"ℹ️ Mensaje no manejado tipo: {msg_type}")
                send_message_async(sender, "⚠️ Lo siento, solo puedo procesar texto, imágenes, documentos y audio por ahora.")
    
    return jsonify({"status": "ok"}), 200

def handle_text_message(sender, message, client_name, client_data):
    """Maneja mensajes de texto"""
    text = message.get("text", {}).get("body", "").strip()
    text_lower = text.lower()
    
    logger.info(f"✉️ Texto recibido de {sender}: {text}")
    
    # Registrar interacción
    register_new_interaction(sender, "Mensaje Texto", text)
    
    # Comando especial GPT
    if text_lower.startswith("sgpt:"):
        gpt_query = text[5:].strip()
        gpt_response = ask_gpt(gpt_query)
        if gpt_response:
            send_message_async(sender, gpt_response)
        else:
            send_message_async(sender, "⚠️ No pude procesar tu consulta en este momento. Intenta más tarde.")
        return
    
    # Menú principal
    if text_lower in ["hola", "hi", "hello", "menú", "menu"]:
        send_main_menu(sender, client_name)
        GREETED_USERS[sender] = datetime.now().timestamp()
        return
    
    # Opciones del menú
    option_handlers = {
        "1": handle_policy_renewal,
        "2": handle_auto_documents,
        "3": handle_promotions,
        "4": handle_follow_up,
        "5": lambda to, phone, msg: send_message_async(to, "📞 Para préstamos IMSS, Christian te contactará con las mejores tasas."),
        "6": lambda to, phone, msg: send_message_async(to, "🏥 VRIM: Cobertura médica familiar. Christian te dará todos los detalles."),
        "7": handle_contact_advisor
    }
    
    # Verificar si es una opción numérica
    if text in option_handlers:
        option_handlers[text](sender, sender, text)
        return
    
    # Intentar manejar con flujos específicos
    handlers = [
        handle_policy_renewal,
        handle_contact_advisor
    ]
    
    for handler in handlers:
        if handler(sender, sender, text):
            return
    
    # Si ya fue saludado pero no entendemos el mensaje
    if sender in GREETED_USERS:
        # Usar GPT como fallback para mensajes naturales
        if len(text.split()) >= 3 and any(c.isalpha() for c in text):
            gpt_response = ask_gpt(f"El cliente dice: '{text}'. Responde brevemente como asistente de seguros.")
            if gpt_response:
                send_message_async(sender, gpt_response)
                return
        
        # Fallback final
        send_message_async(sender, 
            "❓ No entendí tu mensaje. Por favor elige una opción del 1 al 7 o escribe 'menu' para ver las opciones."
        )
    else:
        # Primer mensaje, enviar menú
        send_main_menu(sender, client_name)
        GREETED_USERS[sender] = datetime.now().timestamp()

def handle_media_message(sender, message, msg_type, client_name):
    """Maneja mensajes multimedia"""
    media_info = None
    file_extension = ""
    mime_type = ""
    
    if msg_type == "image":
        media_info = message.get("image", {})
        file_extension = ".jpg"
        mime_type = "image/jpeg"
    elif msg_type == "document":
        media_info = message.get("document", {})
        filename = media_info.get("filename", "documento")
        file_extension = "." + filename.split(".")[-1] if "." in filename else ""
        mime_type = media_info.get("mime_type", "application/octet-stream")
    elif msg_type == "audio":
        media_info = message.get("audio", {})
        file_extension = ".ogg"
        mime_type = "audio/ogg"
    
    if not media_info:
        send_message_async(sender, "⚠️ No pude procesar el archivo. Intenta nuevamente.")
        return
    
    media_id = media_info.get("id")
    if not media_id:
        send_message_async(sender, "⚠️ Error al obtener el archivo.")
        return
    
    # Descargar media
    try:
        # Obtener URL del media
        url = f"https://graph.facebook.com/v21.0/{media_id}"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        media_response = requests.get(url, headers=headers, timeout=10)
        
        if media_response.status_code != 200:
            send_message_async(sender, "⚠️ Error al descargar el archivo.")
            return
        
        media_data = media_response.json()
        media_url = media_data.get("url")
        
        if not media_url:
            send_message_async(sender, "⚠️ Error al obtener URL del archivo.")
            return
        
        # Descargar contenido
        download_response = requests.get(media_url, headers=headers, timeout=15)
        if download_response.status_code != 200:
            send_message_async(sender, "⚠️ Error al descargar el contenido.")
            return
        
        file_bytes = download_response.content
        
        # Crear nombre de archivo
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{msg_type}_{timestamp}{file_extension}"
        
        # Guardar en Drive
        drive_url = save_to_drive(file_bytes, filename, sender, mime_type)
        
        if drive_url:
            # Notificar al asesor si es documento importante
            if msg_type in ["image", "document"] and ADVISOR_NUMBER:
                advisor_msg = (
                    f"📎 *Nuevo archivo recibido*\n\n"
                    f"👤 De: {client_name or sender}\n"
                    f"📱 Tel: {sender}\n"
                    f"📂 Tipo: {msg_type}\n"
                    f"🔗 Drive: {drive_url}"
                )
                send_message_async(ADVISOR_NUMBER, advisor_msg)
            
            # Confirmar al cliente
            confirmation_msg = (
                f"✅ ¡Gracias! He recibido tu {msg_type} y lo he guardado "
                f"de forma segura en tu expediente."
            )
            
            # Mensaje adicional según contexto
            context = USER_CONTEXT.get(sender, {}).get("context")
            if context == "awaiting_auto_docs":
                confirmation_msg += (
                    "\n\n¿Tienes más documentos para enviar o prefieres "
                    "que proceda con la cotización?"
                )
            
            send_message_async(sender, confirmation_msg)
            register_new_interaction(sender, f"Archivo {msg_type} Recibido", f"Guardado en Drive: {drive_url}")
        
        else:
            send_message_async(sender, "⚠️ Recibí tu archivo pero hubo un error al guardarlo. Intenta más tarde.")
    
    except Exception as e:
        logger.error(f"❌ Error procesando media: {e}")
        send_message_async(sender, "⚠️ Ocurrió un error al procesar tu archivo. Intenta más tarde.")

# ---------------------------------------------------------------
# SECCIÓN: ENDPOINTS AUXILIARES
# ---------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    """Endpoint de salud"""
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()}), 200

@app.route("/ext/test-send", methods=["POST"])
def test_send():
    """Endpoint para probar envío de mensajes"""
    try:
        data = request.get_json()
        to = data.get("to")
        text = data.get("text")
        
        if not to or not text:
            return jsonify({"error": "Faltan parámetros 'to' o 'text'"}), 400
        
        result = send_message(to, text)
        return jsonify({"success": result is not None}), 200
    
    except Exception as e:
        logger.error(f"❌ Error en test-send: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/ext/send-promo", methods=["POST"])
def send_promo():
    """Endpoint para envío masivo de promociones"""
    def _send_promo_async():
        try:
            data = request.get_json()
            template_name = data.get("template", "promocion_secom")
            phones = data.get("phones", [])
            
            if not phones:
                logger.error("❌ No se proporcionaron números para envío masivo")
                return
            
            success_count = 0
            for phone in phones:
                try:
                    result = send_template(template_name, phone)
                    if result and result.status_code == 200:
                        success_count += 1
                    
                    # Pequeña pausa para evitar rate limiting
                    threading.Event().wait(0.5)
                    
                except Exception as e:
                    logger.error(f"❌ Error enviando a {phone}: {e}")
            
            logger.info(f"✅ Envío masivo completado: {success_count}/{len(phones)} exitosos")
            
            # Notificar al asesor
            if ADVISOR_NUMBER:
                summary_msg = (
                    f"📊 *Resumen Envío Masivo*\n\n"
                    f"📤 Enviados: {len(phones)}\n"
                    f"✅ Exitosos: {success_count}\n"
                    f"❌ Fallidos: {len(phones) - success_count}\n"
                    f"⏰ Hora: {datetime.now().strftime('%d/%m/%Y %H:%M')}"
                )
                send_message(ADVISOR_NUMBER, summary_msg)
                
        except Exception as e:
            logger.error(f"❌ Error en envío masivo: {e}")
    
    # Ejecutar en hilo separado para evitar timeout
    thread = threading.Thread(target=_send_promo_async)
    thread.daemon = True
    thread.start()
    
    return jsonify({"status": "procesando", "message": "Envío masivo iniciado en segundo plano"}), 202

# ---------------------------------------------------------------
# EJECUCIÓN PRINCIPAL
# ---------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    
    logger.info(f"🚀 Iniciando Vicky Bot SECOM en puerto {port}")
    logger.info(f"📱 Phone Number ID: {PHONE_NUMBER_ID}")
    logger.info(f"👤 Advisor Number: {ADVISOR_NUMBER}")
    logger.info(f"📊 Sheets ID: {SHEETS_ID_LEADS}")
    logger.info(f"📁 Drive Folder: {DRIVE_FOLDER_ID}")
    
    app.run(host="0.0.0.0", port=port, debug=debug)

