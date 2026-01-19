from flask import Flask, request, jsonify
from flask_cors import CORS
import json
import re
import os
from datetime import datetime
import google.generativeai as genai
from simple_salesforce import Salesforce
import secrets
import hashlib

app = Flask(__name__)
CORS(app)

# Restaurant Configuration
RESTAURANT_NAME = "Twilight Cafe"
ASSISTANT_NAME = "Plato"

# Configure Gemini API
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    generation_config = {
        "temperature": 0.8,
        "top_p": 0.95,
        "top_k": 40,
        "max_output_tokens": 256,
    }
    model = genai.GenerativeModel('gemini-2.5-flash', generation_config=generation_config)
else:
    print("WARNING: GEMINI_API_KEY not found.")
    model = None

# Configure Salesforce
SF_USERNAME = os.getenv('SF_USERNAME', '')
SF_PASSWORD = os.getenv('SF_PASSWORD', '')
SF_SECURITY_TOKEN = os.getenv('SF_SECURITY_TOKEN', '')
SF_DOMAIN = os.getenv('SF_DOMAIN', 'login')

sf = None
if SF_USERNAME and SF_PASSWORD and SF_SECURITY_TOKEN:
    try:
        sf = Salesforce(
            username=SF_USERNAME,
            password=SF_PASSWORD,
            security_token=SF_SECURITY_TOKEN,
            domain=SF_DOMAIN
        )
        print("✅ Salesforce connected successfully!")
    except Exception as e:
        print(f"❌ Salesforce connection error: {e}")
        sf = None
else:
    print("⚠️  Salesforce credentials not configured.")

# Menu items
MENU = {
    'burger': {'name': 'Burger', 'price': 8.99},
    'cheeseburger': {'name': 'Cheeseburger', 'price': 9.99},
    'pizza': {'name': 'Pizza', 'price': 12.99},
    'pasta': {'name': 'Pasta', 'price': 10.99},
    'salad': {'name': 'Salad', 'price': 7.99},
    'fries': {'name': 'Fries', 'price': 3.99},
    'chicken wings': {'name': 'Chicken Wings', 'price': 11.99},
    'sandwich': {'name': 'Sandwich', 'price': 6.99},
    'soda': {'name': 'Soda', 'price': 2.99},
    'water': {'name': 'Water', 'price': 1.99},
    'coffee': {'name': 'Coffee', 'price': 3.49},
    'milkshake': {'name': 'Milkshake', 'price': 5.99}
}

# In-memory storage
carts = {}
conversation_history = {}
sessions = {}
completed_orders = {}

# User Authentication Functions
def hash_password(password):
    """Hash password using SHA256"""
    return hashlib.sha256(password.encode()).hexdigest()

def create_user_in_salesforce(name, email, phone, password):
    """Create a new user (customer) in Salesforce"""
    if not sf:
        return None
    
    try:
        query = f"SELECT Id FROM Customer__c WHERE Email__c = '{email}' LIMIT 1"
        result = sf.query(query)
        
        if result['totalSize'] > 0:
            return {'error': 'User already exists', 'exists': True}
        
        customer_data = {
            'Name': name,
            'Email__c': email,
            'Phone__c': phone,
            'Password_Hash__c': hash_password(password),
            'Created_Date__c': datetime.now().isoformat()
        }
        
        customer_result = sf.Customer__c.create(customer_data)
        customer_id = customer_result['id']
        
        print(f"✅ User created in Salesforce: {customer_id}")
        return {
            'customer_id': customer_id,
            'name': name,
            'email': email
        }
    
    except Exception as e:
        print(f"❌ Error creating user: {e}")
        return {'error': str(e)}

def authenticate_user(email, password):
    """Authenticate user against Salesforce"""
    if not sf:
        return None
    
    try:
        query = f"SELECT Id, Name, Email__c, Phone__c, Password_Hash__c FROM Customer__c WHERE Email__c = '{email}' LIMIT 1"
        result = sf.query(query)
        
        if result['totalSize'] == 0:
            return {'error': 'Invalid email or password'}
        
        customer = result['records'][0]
        
        password_hash = hash_password(password)
        stored_hash = customer.get('Password_Hash__c', '')
        
        if password_hash != stored_hash:
            return {'error': 'Invalid email or password'}
        
        session_token = secrets.token_urlsafe(32)
        
        sessions[session_token] = {
            'customer_id': customer['Id'],
            'name': customer['Name'],
            'email': customer['Email__c'],
            'logged_in_at': datetime.now().isoformat()
        }
        
        print(f"✅ User authenticated: {customer['Name']}")
        return {
            'session_token': session_token,
            'customer_id': customer['Id'],
            'name': customer['Name'],
            'email': customer['Email__c']
        }
    
    except Exception as e:
        print(f"❌ Authentication error: {e}")
        return {'error': str(e)}

def get_user_from_session(session_token):
    """Get user info from session token"""
    return sessions.get(session_token)

def save_order_to_salesforce(customer_id, session_id, cart_items, total, status='Completed'):
    """Save order to Salesforce - ONLY called on checkout"""
    if not sf:
        return None
    
    try:
        order_data = {
            'Customer__c': customer_id,
            'Session_ID__c': session_id,
            'Total_Amount__c': total,
            'Order_Status__c': status,
            'Restaurant_Name__c': RESTAURANT_NAME,
            'Order_Date__c': datetime.now().isoformat()
        }
        
        order_result = sf.Order__c.create(order_data)
        order_id = order_result['id']
        
        for item in cart_items:
            item_data = {
                'Order__c': order_id,
                'Item_Name__c': item['name'],
                'Quantity__c': item['quantity'],
                'Unit_Price__c': item['price'],
                'Total_Price__c': item['price'] * item['quantity']
            }
            sf.Order_Item__c.create(item_data)
        
        print(f"✅ Order saved to Salesforce: {order_id}")
        return order_id
    
    except Exception as e:
        print(f"❌ Error saving order: {e}")
        return None

def get_user_orders(customer_id):
    """Get all orders for a customer"""
    if not sf:
        return []
    
    try:
        query = f"""
        SELECT Id, Total_Amount__c, Order_Status__c, Order_Date__c 
        FROM Order__c 
        WHERE Customer__c = '{customer_id}' 
        ORDER BY Order_Date__c DESC
        """
        result = sf.query(query)
        
        orders = []
        for order in result['records']:
            items_query = f"""
            SELECT Item_Name__c, Quantity__c, Unit_Price__c 
            FROM Order_Item__c 
            WHERE Order__c = '{order['Id']}'
            """
            items_result = sf.query(items_query)
            
            items = []
            for item in items_result['records']:
                items.append({
                    'name': item['Item_Name__c'],
                    'quantity': int(item['Quantity__c']),
                    'price': float(item['Unit_Price__c'])
                })
            
            orders.append({
                'order_id': order['Id'],
                'total': float(order['Total_Amount__c']),
                'status': order['Order_Status__c'],
                'date': order['Order_Date__c'],
                'items': items
            })
        
        return orders
    
    except Exception as e:
        print(f"❌ Error getting orders: {e}")
        return []

def is_greeting_or_casual(text):
    casual_patterns = [
        r'^hi+$', r'^hello+$', r'^hey+$', r'^good morning$', r'^good afternoon$',
        r'^good evening$', r'^how are you$', r'^thanks$', r'^thank you$',
        r'^okay$', r'^ok$', r'^yes$', r'^no$', r'^sure$', r'^alright$', r'^please$'
    ]
    text_lower = text.lower().strip()
    for pattern in casual_patterns:
        if re.match(pattern, text_lower):
            return True
    return False

def is_checkout_command(text):
    """Check if user wants to checkout"""
    text_lower = text.lower().strip()
    checkout_phrases = [
        'checkout', 'check out', 'complete order', 'complete my order',
        'finish order', 'finish my order', 'place order', 'place my order',
        "that's all", "that is all", "i'm done", "im done", 'done ordering',
        'finish', 'complete', 'thats it', "that's it"
    ]
    return any(phrase in text_lower for phrase in checkout_phrases)

def extract_order_with_gemini(user_text):
    if is_greeting_or_casual(user_text):
        return []
    
    if is_checkout_command(user_text):
        return []
    
    if not model:
        return fallback_extract_order(user_text)
    
    menu_items_list = list(MENU.keys())
    menu_text = ", ".join(menu_items_list)
    
    prompt = f"""Extract food items and quantities.

Menu: {menu_text}

User: "{user_text}"

IMPORTANT Rules:
- "a burger" = burger, quantity 1
- "two burgers" = burger, quantity 2
- "three more coffees" = coffee, quantity 3
- "add 5 fries" = fries, quantity 5
- If user says "more", still count the quantity correctly
- Return [] if no items found

Return ONLY valid JSON array:
[{{"item": "exact_menu_item", "quantity": number}}]

Examples:
"I want three coffees" -> [{{"item": "coffee", "quantity": 3}}]
"add two more burgers" -> [{{"item": "burger", "quantity": 2}}]
"five fries please" -> [{{"item": "fries", "quantity": 5}}]

JSON response:"""

    try:
        response = model.generate_content(prompt)
        response_text = response.text.strip()
        response_text = re.sub(r'```json\s*', '', response_text)
        response_text = re.sub(r'```\s*', '', response_text)
        json_match = re.search(r'\[.*?\]', response_text, re.DOTALL)
        if json_match:
            response_text = json_match.group(0)
        items = json.loads(response_text)
        
        valid_items = []
        for item in items:
            item_name = item.get('item', '').lower().strip()
            quantity = int(item.get('quantity', 1))
            if item_name in MENU:
                valid_items.append({
                    'key': item_name,
                    'name': MENU[item_name]['name'],
                    'price': MENU[item_name]['price'],
                    'quantity': quantity
                })
        return valid_items
    except Exception as e:
        print(f"Gemini error: {e}")
        return fallback_extract_order(user_text)

def fallback_extract_order(user_text):
    """Enhanced fallback with better quantity detection"""
    items = []
    text_lower = user_text.lower()
    
    quantity_map = {
        'a': 1, 'an': 1, 'one': 1, 
        'two': 2, 'three': 3, 'four': 4, 'five': 5,
        'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10
    }
    
    for menu_key in MENU.keys():
        if menu_key in text_lower:
            quantity = 1
            
            words = text_lower.split()
            for i, word in enumerate(words):
                if menu_key.split()[0] in word:
                    if i > 0:
                        prev_word = words[i-1]
                        if prev_word in quantity_map:
                            quantity = quantity_map[prev_word]
                        elif prev_word.isdigit():
                            quantity = int(prev_word)
                        elif i > 1 and words[i-1] == 'more':
                            if words[i-2] in quantity_map:
                                quantity = quantity_map[words[i-2]]
                            elif words[i-2].isdigit():
                                quantity = int(words[i-2])
                    break
            
            items.append({
                'key': menu_key,
                'name': MENU[menu_key]['name'],
                'price': MENU[menu_key]['price'],
                'quantity': quantity
            })
    
    return items

def generate_response_with_gemini(cart_items, added_items, total, action='add', user_text='', user_name=''):
    if not model:
        return get_fallback_response(action, added_items, total, user_name)
    
    name_greeting = f" {user_name}" if user_name else ""
    
    if action == 'welcome':
        prompt = f"""You are {ASSISTANT_NAME}, a friendly server at {RESTAURANT_NAME}.
Greet the customer{name_greeting} warmly and ask what they would like to order.
Keep it to 2-3 sentences maximum.
Be natural and conversational."""
        
    elif action == 'add' and added_items:
        items_text = ', '.join([f"{item['quantity']} {item['name']}" for item in added_items])
        prompt = f"""You are {ASSISTANT_NAME}, a friendly server at {RESTAURANT_NAME}.

Customer just ordered: {items_text}
Their new cart total is: ${total:.2f}

Respond in EXACTLY 2-3 complete sentences:
1. Enthusiastically confirm what was added
2. State the new total clearly
3. Ask if they want anything else

Be conversational and natural. Complete all sentences."""
        
    elif action == 'checkout':
        prompt = f"""You are {ASSISTANT_NAME}, a friendly server at {RESTAURANT_NAME}.

The customer is completing their order.
Final total: ${total:.2f}

Respond in EXACTLY 2-3 complete sentences:
1. Thank them for their order
2. Confirm the total amount
3. Let them know the food will be ready soon

Be warm and appreciative. Complete all sentences."""
        
    elif action == 'no_items':
        return "Sorry, I didn't catch that! Could you repeat what you'd like to order?"
    else:
        return "What can I get for you today?"
    
    try:
        response = model.generate_content(
            prompt,
            safety_settings={
                'HARASSMENT': 'block_none',
                'HATE_SPEECH': 'block_none',
                'SEXUALLY_EXPLICIT': 'block_none',
                'DANGEROUS_CONTENT': 'block_none'
            }
        )
        
        result = response.text.strip()
        result = result.strip('"\'')
        
        if result and not result[-1] in '.!?':
            result += '.'
        
        return result if result else get_fallback_response(action, added_items, total, user_name)
    
    except Exception as e:
        print(f"Gemini error: {e}")
        return get_fallback_response(action, added_items, total, user_name)

def get_fallback_response(action, added_items=None, total=0, user_name=''):
    """High-quality fallback responses"""
    name_greeting = f" {user_name}" if user_name else ""
    
    if action == 'welcome':
        return f"Hey{name_greeting}! Welcome to {RESTAURANT_NAME}! I'm {ASSISTANT_NAME}, and I'll be taking care of you today. What can I get started for you?"
    
    elif action == 'add' and added_items:
        items_text = ', '.join([f"{item['quantity']} {item['name']}" for item in added_items])
        return f"Perfect! I've added {items_text} to your order. Your new total is ${total:.2f}. Would you like anything else?"
    
    elif action == 'checkout':
        return f"Awesome! Thanks so much for your order. Your total comes to ${total:.2f}. We'll have that ready for you in just a few minutes!"
    
    elif action == 'no_items':
        return "Sorry, I didn't quite catch that! Could you tell me what you'd like to order?"
    
    return "What can I get for you today?"

# API Routes
@app.route('/', methods=['GET'])
def home():
    return jsonify({
        'status': 'running',
        'restaurant': RESTAURANT_NAME,
        'assistant': ASSISTANT_NAME,
        'gemini_configured': bool(GEMINI_API_KEY),
        'salesforce_connected': bool(sf),
        'multi_user_enabled': True
    })

@app.route('/api/auth/signup', methods=['POST'])
def signup():
    data = request.json
    name = data.get('name')
    email = data.get('email')
    phone = data.get('phone', '')
    password = data.get('password')
    
    if not all([name, email, password]):
        return jsonify({'error': 'Missing required fields'}), 400
    
    result = create_user_in_salesforce(name, email, phone, password)
    
    if result and 'error' in result:
        return jsonify(result), 400
    
    return jsonify({
        'success': True,
        'message': 'Account created successfully!',
        'customer': result
    })

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email')
    password = data.get('password')
    
    if not all([email, password]):
        return jsonify({'error': 'Missing email or password'}), 400
    
    result = authenticate_user(email, password)
    
    if result and 'error' in result:
        return jsonify(result), 401
    
    return jsonify({
        'success': True,
        'message': 'Login successful!',
        'session_token': result['session_token'],
        'user': {
            'customer_id': result['customer_id'],
            'name': result['name'],
            'email': result['email']
        }
    })

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    data = request.json
    session_token = data.get('session_token')
    
    if session_token in sessions:
        del sessions[session_token]
    
    return jsonify({'success': True, 'message': 'Logged out successfully'})

@app.route('/api/auth/me', methods=['GET'])
def get_current_user():
    session_token = request.headers.get('Authorization', '').replace('Bearer ', '')
    user = get_user_from_session(session_token)
    
    if not user:
        return jsonify({'error': 'Not authenticated'}), 401
    
    return jsonify({'user': user})

@app.route('/api/orders/history', methods=['GET'])
def get_order_history():
    session_token = request.headers.get('Authorization', '').replace('Bearer ', '')
    user = get_user_from_session(session_token)
    
    if not user:
        return jsonify({'error': 'Not authenticated'}), 401
    
    orders = get_user_orders(user['customer_id'])
    return jsonify({'orders': orders})

@app.route('/api/menu', methods=['GET'])
def get_menu():
    return jsonify({'menu': MENU})

@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify({
        'restaurant_name': RESTAURANT_NAME,
        'assistant_name': ASSISTANT_NAME
    })

@app.route('/api/process-order', methods=['POST'])
def process_order():
    """Process voice order"""
    data = request.json
    user_text = data.get('text', '')
    session_id = data.get('session_id', 'default')
    session_token = data.get('session_token')
    
    user = get_user_from_session(session_token) if session_token else None
    customer_id = user['customer_id'] if user else None
    user_name = user['name'] if user else None
    
    print(f"Processing - User: {user_name}, Said: '{user_text}'")
    
    if session_id not in carts:
        carts[session_id] = []
    if session_id not in conversation_history:
        conversation_history[session_id] = []
    
    conversation_history[session_id].append(f"Customer: {user_text}")
    
    if is_greeting_or_casual(user_text):
        response_text = generate_response_with_gemini([], [], 0, action='welcome', user_name=user_name)
        return jsonify({
            'success': True,
            'cart': carts[session_id],
            'total': sum(item['price'] * item['quantity'] for item in carts[session_id]),
            'response': response_text,
            'items_added': []
        })
    
    if 'clear' in user_text.lower() and 'cart' in user_text.lower():
        carts[session_id] = []
        return jsonify({
            'success': True,
            'cart': [],
            'total': 0,
            'response': "Cart cleared! What would you like to order?"
        })
    
    if is_checkout_command(user_text):
        if not carts[session_id]:
            return jsonify({
                'success': False,
                'cart': [],
                'total': 0,
                'response': "Your cart is empty! What would you like to order?"
            })
        
        total = sum(item['price'] * item['quantity'] for item in carts[session_id])
        response_text = generate_response_with_gemini(carts[session_id], [], total, action='checkout', user_name=user_name)
        
        order_id = None
        if sf and customer_id:
            order_id = save_order_to_salesforce(customer_id, session_id, carts[session_id], total, 'Completed')
            print(f"✅ Order completed and saved: {order_id}")
        
        completed_orders[session_id] = True
        
        return jsonify({
            'success': True,
            'cart': carts[session_id],
            'total': total,
            'response': response_text,
            'checkout': True,
            'order_id': order_id
        })
    
    extracted_items = extract_order_with_gemini(user_text)
    
    if not extracted_items:
        return jsonify({
            'success': False,
            'cart': carts[session_id],
            'total': sum(item['price'] * item['quantity'] for item in carts[session_id]),
            'response': generate_response_with_gemini([], [], 0, action='no_items')
        })
    
    if completed_orders.get(session_id, False):
        print(f"🔄 Previous order completed, starting new cart for session {session_id}")
        carts[session_id] = []
        completed_orders[session_id] = False
    
    for item in extracted_items:
        existing = next((x for x in carts[session_id] if x['key'] == item['key']), None)
        if existing:
            existing['quantity'] += item['quantity']
        else:
            carts[session_id].append(item)
    
    total = sum(item['price'] * item['quantity'] for item in carts[session_id])
    response_text = generate_response_with_gemini(carts[session_id], extracted_items, total, action='add', user_name=user_name)
    
    print(f"✅ Added to cart: {extracted_items}")
    
    return jsonify({
        'success': True,
        'cart': carts[session_id],
        'total': total,
        'response': response_text,
        'items_added': extracted_items
    })

@app.route('/api/text-to-speech', methods=['POST'])
def text_to_speech():
    """Convert text to speech using ElevenLabs API"""
    data = request.json
    text = data.get('text', '')
    
    ELEVENLABS_API_KEY = os.getenv('ELEVENLABS_API_KEY', '')
    
    if not ELEVENLABS_API_KEY:
        print("❌ ElevenLabs API key not configured")
        return jsonify({'error': 'ElevenLabs API key not configured', 'success': False}), 400
    
    try:
        import requests
        import base64
        
        # VOICE OPTIONS - Choose one from the list below:
        # Popular voices:
        # "21m00Tcm4TlvDq8ikWAM" - Rachel (friendly female)
        # "EXAVITQu4vr4xnSDxMaL" - Bella (expressive female)
        # "ErXwobaYiN019PkySvjV" - Antoni (well-rounded male)
        # "VR6AewLTigWG4xSOukaG" - Arnold (crisp male)
        # "pNInz6obpgDQGcFmaJgB" - Adam (deep male)
        # "yoZ06aMxZJJ28mfd3POQ" - Sam (dynamic male)
        # "AZnzlk1XvdvUeBnXmlld" - Domi (strong female)
        # "MF3mGyEYCl7XYWbV9V6O" - Elli (emotional female)
        # "TxGEqnHWrfWFTfGW9XjX" - Josh (young male)
        # "jBpfuIE2acCO8z3wKNLl" - Gigi (childish female)
        # "onwK4e9ZLuTAKqWW03F9" - Daniel (authoritative male)
        
        # Set your preferred voice here:
        voice_id = os.getenv('ELEVENLABS_VOICE_ID', '21m00Tcm4TlvDq8ikWAM')  # Default: Rachel
        
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        
        headers = {
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": ELEVENLABS_API_KEY
        }
        
        payload = {
            "text": text,
            "model_id": "eleven_monolingual_v1",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True
            }
        }
        
        print(f"🎤 Generating TTS with voice {voice_id} for: {text[:50]}...")
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        
        if response.status_code == 200:
            audio_base64 = base64.b64encode(response.content).decode('utf-8')
            print(f"✅ TTS generated successfully ({len(audio_base64)} chars)")
            return jsonify({
                'success': True,
                'audio': audio_base64
            })
        else:
            error_msg = f"ElevenLabs API error: {response.status_code} - {response.text}"
            print(f"❌ {error_msg}")
            return jsonify({'error': error_msg, 'success': False}), 500
            
    except Exception as e:
        error_msg = f"ElevenLabs TTS error: {str(e)}"
        print(f"❌ {error_msg}")
        return jsonify({'error': error_msg, 'success': False}), 500

@app.route('/api/welcome', methods=['GET'])
def get_welcome():
    session_token = request.headers.get('Authorization', '').replace('Bearer ', '')
    user = get_user_from_session(session_token)
    user_name = user['name'] if user else None
    
    response_text = generate_response_with_gemini([], [], 0, action='welcome', user_name=user_name)
    return jsonify({'response': response_text})

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
