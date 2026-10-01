import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

class WhatsAppService:
    def __init__(self):
        self.access_token = getattr(settings, 'WHATSAPP_API_TOKEN', None)
        self.phone_number_id = getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', None)
        self.base_url = f"https://graph.facebook.com/v19.0/{self.phone_number_id}/messages"

    def is_configured(self):
        return bool(self.access_token and self.phone_number_id)

    def _format_phone(self, phone):
        # Clean phone number for WhatsApp (remove + and spaces, etc.)
        cleaned = "".join(filter(str.isdigit, str(phone)))
        # Default to India country code if no country code provided and length is 10
        if len(cleaned) == 10:
            cleaned = "91" + cleaned
        return cleaned

    def send_template_message(self, to_phone, template_name, language_code="en", variables=None):
        """
        Sends a WhatsApp template message using Meta Cloud API.
        `variables` is a list of strings representing the dynamic parameters in the template.
        """
        if not self.is_configured():
            logger.info(f"Mocking WhatsApp template '{template_name}' to {to_phone} with args {variables}")
            return {"status": "mocked", "message_id": f"mock_wa_msg_{to_phone}"}

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

        components = []
        if variables:
            parameters = [{"type": "text", "text": str(v)} for v in variables]
            components.append({
                "type": "body",
                "parameters": parameters
            })

        payload = {
            "messaging_product": "whatsapp",
            "to": self._format_phone(to_phone),
            "type": "template",
            "template": {
                "name": template_name,
                "language": {
                    "code": language_code
                },
                "components": components
            }
        }

        try:
            response = requests.post(self.base_url, headers=headers, json=payload, timeout=10)
            response.raise_for_status()
            data = response.json()
            message_id = data.get("messages", [{}])[0].get("id")
            return {"status": "sent", "message_id": message_id, "raw_response": data}
        except requests.exceptions.RequestException as e:
            err_msg = str(e)
            if hasattr(e, 'response') and e.response is not None:
                err_msg = e.response.text
            logger.error(f"WhatsApp API Error: {err_msg}")
            return {"status": "error", "error": err_msg}

    def send_text_message(self, to_phone, text):
        """
        Sends a free-form WhatsApp text message. 
        Note: This only works if a 24-hour service window is open with the user.
        """
        if not self.is_configured():
            logger.info(f"Mocking WhatsApp text to {to_phone}: {text}")
            return {"status": "mocked", "message_id": f"mock_wa_msg_{to_phone}"}

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

        payload = {
            "messaging_product": "whatsapp",
            "to": self._format_phone(to_phone),
            "type": "text",
            "text": {
                "preview_url": False,
                "body": text
            }
        }

        try:
            response = requests.post(self.base_url, headers=headers, json=payload, timeout=10)
            response.raise_for_status()
            data = response.json()
            message_id = data.get("messages", [{}])[0].get("id")
            return {"status": "sent", "message_id": message_id, "raw_response": data}
        except requests.exceptions.RequestException as e:
            err_msg = str(e)
            if hasattr(e, 'response') and e.response is not None:
                err_msg = e.response.text
            logger.error(f"WhatsApp API Error: {err_msg}")
            return {"status": "error", "error": err_msg}
