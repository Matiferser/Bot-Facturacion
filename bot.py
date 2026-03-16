import os
from flask import Flask, request, jsonify
import anthropic
import requests
import json

app = Flask(__name__)

# ============================================================
# CONFIGURACIÓN - Se leen desde las variables de entorno de Railway
# ============================================================
META_TOKEN = os.environ.get("META_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "bot_facturacion_verify")

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")

AFIP_SDK_API_KEY = os.environ.get("AFIP_SDK_API_KEY")
AFIP_SDK_URL = "https://api.afipsdk.com"

TU_CUIT = os.environ.get("TU_CUIT")

# Números autorizados (sin el +, ej: 5493624123456)
NUMEROS_AUTORIZADOS = os.environ.get("NUMEROS_AUTORIZADOS", "").split(",")
# ============================================================

claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)


def enviar_whatsapp(numero_destino, mensaje):
    """Envía mensaje por WhatsApp via Meta API."""
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": numero_destino,
        "type": "text",
        "text": {"body": mensaje}
    }
    response = requests.post(url, headers=headers, json=payload)
    return response.json()


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
        "tipo_comprobante": 11,
        "punto_venta": 1,
        "concepto": 2,
        "doc_tipo": 96 if datos.get("cuit_cliente") is None else 80,
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

    return response.json()


@app.route("/webhook", methods=["GET"])
def verificar_webhook():
    """Verificación del webhook por parte de Meta."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Token inválido", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    """Recibe mensajes de WhatsApp y procesa la factura."""
    data = request.get_json()

    numero_origen = None
    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        if "messages" not in value:
            return jsonify({"status": "ok"}), 200

        mensaje_obj = value["messages"][0]
        numero_origen = mensaje_obj["from"]
        mensaje_entrante = mensaje_obj["text"]["body"].strip()

        # Verificar número autorizado
        if NUMEROS_AUTORIZADOS and NUMEROS_AUTORIZADOS[0]:
            if numero_origen not in NUMEROS_AUTORIZADOS:
                enviar_whatsapp(numero_origen, "⛔ No tenés permiso para usar este bot.")
                return jsonify({"status": "ok"}), 200

        # Verificar que parece una solicitud de factura
        palabras_clave = ["factur", "cobr", "ticket", "comprobante"]
        es_factura = any(p in mensaje_entrante.lower() for p in palabras_clave)

        if not es_factura:
            enviar_whatsapp(
                numero_origen,
                "Hola! Soy tu bot de facturación 🧾\n\nMandame un mensaje como:\n\"Facturá $50.000 a Juan Pérez, CUIT 20-12345678-9, por consultoría de marketing\""
            )
            return jsonify({"status": "ok"}), 200

        enviar_whatsapp(numero_origen, "⏳ Procesando tu factura, un momento...")

        datos = extraer_datos_factura(mensaje_entrante)

        confirmacion = (
            f"📋 Datos de la factura:\n"
            f"• Cliente: {datos['nombre_cliente']}\n"
            f"• CUIT: {datos.get('cuit_cliente') or 'No especificado'}\n"
            f"• Monto: ${datos['monto']:,.0f}\n"
            f"• Descripción: {datos['descripcion']}\n\n"
            f"Emitiendo factura C... ✅"
        )
        enviar_whatsapp(numero_origen, confirmacion)

        resultado = emitir_factura(datos)

        if resultado.get("success") or resultado.get("CAE"):
            cae = resultado.get("CAE") or resultado.get("cae")
            pdf_url = resultado.get("pdf_url") or resultado.get("url_pdf")

            mensaje_exito = (
                f"✅ Factura emitida correctamente!\n"
                f"• CAE: {cae}\n"
                f"• Monto: ${datos['monto']:,.0f}\n"
                f"• Cliente: {datos['nombre_cliente']}\n"
            )
            if pdf_url:
                mensaje_exito += f"• PDF: {pdf_url}"

            enviar_whatsapp(numero_origen, mensaje_exito)
        else:
            error = resultado.get("message") or resultado.get("error") or str(resultado)
            enviar_whatsapp(numero_origen, f"❌ Error al emitir la factura: {error}")

    except json.JSONDecodeError:
        if numero_origen:
            enviar_whatsapp(numero_origen, "No pude entender el mensaje. Probá con:\n\"Facturá $50.000 a Juan Pérez por consultoría\"")
    except Exception as e:
        print(f"Error: {str(e)}")

    return jsonify({"status": "ok"}), 200


@app.route("/", methods=["GET"])
def health():
    return "Bot de facturación funcionando! ✅"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
