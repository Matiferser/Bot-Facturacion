import os
from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
import requests
import json

app = Flask(__name__)

# ============================================================
# CONFIGURACIÓN - Reemplazá con tus keys
# ============================================================
TWILIO_ACCOUNT_SID = "TU_TWILIO_SID"
TWILIO_AUTH_TOKEN = "TU_TWILIO_SECRET"
TWILIO_WHATSAPP_NUMBER = "whatsapp:+14155238886"  # Número sandbox de Twilio

CLAUDE_API_KEY = "TU_CLAUDE_API_KEY"

AFIP_SDK_API_KEY = "TU_AFIP_SDK_API_KEY"
AFIP_SDK_URL = "https://api.afipsdk.com"

# Tus datos fiscales
TU_CUIT = "TU_CUIT_SIN_GUIONES"  # Ejemplo: 20123456789
# ============================================================

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)


def extraer_datos_factura(mensaje):
    """Usa Claude para extraer los datos de factura del mensaje en lenguaje natural."""
    response = claude_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": f"""Extraé los datos de factura del siguiente mensaje y devolvé SOLO un JSON con este formato exacto:
{{
  "nombre_cliente": "nombre completo",
  "cuit_cliente": "cuit sin guiones o null si no hay",
  "monto": numero sin simbolos,
  "descripcion": "descripcion del servicio"
}}

Mensaje: {mensaje}

Devolvé SOLO el JSON, sin explicaciones ni texto adicional."""
            }
        ]
    )
    
    texto = response.content[0].text.strip()
    datos = json.loads(texto)
    return datos


def emitir_factura(datos):
    """Emite la factura C en ARCA via AFIP SDK."""
    headers = {
        "Authorization": f"Bearer {AFIP_SDK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "cuit": TU_CUIT,
        "tipo_comprobante": 11,  # 11 = Factura C
        "punto_venta": 1,
        "concepto": 1,  # 1 = Productos, 2 = Servicios, 3 = Productos y Servicios
        "doc_tipo": 96 if datos.get("cuit_cliente") is None else 80,  # 80=CUIT, 96=DNI
        "doc_nro": datos.get("cuit_cliente") or "0",
        "imp_total": datos["monto"],
        "imp_neto": datos["monto"],
        "detalle": [
            {
                "descripcion": datos["descripcion"],
                "cantidad": 1,
                "precio_unitario": datos["monto"],
                "subtotal": datos["monto"]
            }
        ]
    }
    
    response = requests.post(
        f"{AFIP_SDK_URL}/v1/bill",
        headers=headers,
        json=payload
    )
    
    result = response.json()
    return result


def enviar_whatsapp(numero_destino, mensaje, pdf_url=None):
    """Envía mensaje por WhatsApp via Twilio."""
    params = {
        "from_": TWILIO_WHATSAPP_NUMBER,
        "to": f"whatsapp:{numero_destino}",
        "body": mensaje
    }
    
    if pdf_url:
        params["media_url"] = [pdf_url]
    
    twilio_client.messages.create(**params)


@app.route("/webhook", methods=["POST"])
def webhook():
    """Recibe mensajes de WhatsApp y procesa la factura."""
    mensaje_entrante = request.form.get("Body", "").strip()
    numero_origen = request.form.get("From", "").replace("whatsapp:", "")
    
    resp = MessagingResponse()
    
    try:
        # Verificar que el mensaje parece una solicitud de factura
        palabras_clave = ["factur", "cobr", "ticket", "comprobante"]
        es_factura = any(p in mensaje_entrante.lower() for p in palabras_clave)
        
        if not es_factura:
            resp.message(
                "Hola! Soy tu bot de facturación 🧾\n\n"
                "Mandame un mensaje como:\n"
                "_'Facturá $50.000 a Juan Pérez, CUIT 20-12345678-9, por consultoría de marketing'_"
            )
            return str(resp)
        
        # Notificar que estamos procesando
        enviar_whatsapp(numero_origen, "⏳ Procesando tu factura, un momento...")
        
        # Extraer datos con Claude
        datos = extraer_datos_factura(mensaje_entrante)
        
        # Confirmar datos antes de emitir
        confirmacion = (
            f"📋 *Datos de la factura:*\n"
            f"• Cliente: {datos['nombre_cliente']}\n"
            f"• CUIT: {datos.get('cuit_cliente') or 'No especificado'}\n"
            f"• Monto: ${datos['monto']:,.0f}\n"
            f"• Descripción: {datos['descripcion']}\n\n"
            f"Emitiendo factura C... ✅"
        )
        enviar_whatsapp(numero_origen, confirmacion)
        
        # Emitir factura
        resultado = emitir_factura(datos)
        
        if resultado.get("success") or resultado.get("CAE"):
            cae = resultado.get("CAE") or resultado.get("cae")
            pdf_url = resultado.get("pdf_url") or resultado.get("url_pdf")
            
            mensaje_exito = (
                f"✅ *Factura emitida correctamente!*\n"
                f"• CAE: {cae}\n"
                f"• Monto: ${datos['monto']:,.0f}\n"
                f"• Cliente: {datos['nombre_cliente']}"
            )
            enviar_whatsapp(numero_origen, mensaje_exito, pdf_url)
        else:
            error = resultado.get("message") or resultado.get("error") or "Error desconocido"
            enviar_whatsapp(numero_origen, f"❌ Error al emitir la factura: {error}")
    
    except json.JSONDecodeError:
        enviar_whatsapp(
            numero_origen,
            "No pude entender el mensaje. Probá con:\n"
            "_'Facturá $50.000 a Juan Pérez por consultoría'_"
        )
    except Exception as e:
        enviar_whatsapp(numero_origen, f"❌ Ocurrió un error inesperado: {str(e)}")
    
    return str(resp)


@app.route("/", methods=["GET"])
def health():
    return "Bot de facturación funcionando! ✅"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
