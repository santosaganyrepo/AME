import requests

class SMSSender:
    def __init__(self, api_key):
        self.api_key = api_key
        self.api_url_send = "https://api.smsmobileapi.com/sendsms"  # URL d'envoi
        self.api_url_received = "https://api.smsmobileapi.com/getsms"  # URL pour lire les SMS reçus

    def send_message(self, to, message, sendwa=0, sendsms=1):
        # Préparation des paramètres GET
        params = {
            'apikey': self.api_key,
            'recipients': to,
            'message': message,
            'from': 'python',
            'sendwa': sendwa,  # Ajout du paramètre sendwa
            'sendsms': sendsms  # Ajout du paramètre sendsms
        }
        
        try:
            # Envoi de la requête GET avec les paramètres dans l'URL
            response = requests.get(self.api_url_send, params=params)
            response.raise_for_status()
            return response.json()  # Retourne la réponse de l'API en JSON
        except requests.exceptions.RequestException as e:
            return {"error": str(e)}
    
    def get_received_messages(self):
        # Préparation des paramètres GET
        params = {
            'apikey': self.api_key
        }
        
        try:
            # Envoi de la requête GET avec les paramètres dans l'URL
            response = requests.get(self.api_url_received, params=params)
            response.raise_for_status()
            return response.json()  # Retourne la liste des SMS reçus
        except requests.exceptions.RequestException as e:
            return {"error": str(e)}
