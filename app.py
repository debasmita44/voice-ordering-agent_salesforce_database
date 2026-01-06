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
        "temperature": 0.9,
        "top_p": 0.95,
        "top_k": 40,
        "max_output_tokens": 200,
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

# In-memory storage (fallback)
carts = {}
conversation_history = {}
sessions = {}  # Store active sessions

# User Authentication Functions
def hash_password(password):
    """Hash password using SHA256"""
    return hashlib.sha256(password.encode()).hexdigest()

def create_user_in_salesforce(name, email, phone, password):
    """Create a new user (customer) in Salesforce"""
    if not sf:
        return None
    
    try:
        # Check if user already exists
        query = f"SELECT Id FROM Customer__c WHERE Email__c = '{email}' LIMIT 1"
        result = sf.query(query)
        
        if result['totalSize'] > 0:
            return {'error': 'User already exists', 'exists': True}
        
        # Create new customer
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
        password_hash = hash_password(password)
        query = f"SELECT Id, Name, Email__c, Phone__c FROM Customer__c WHERE Email__c = '{email}' AND Password_Hash__c = '{password_hash}' LIMIT 1"
        result = sf.query(query)
        
        if result['totalSize'] > 0:
            customer = result['records'][0]
            
            # Generate session token
            session_token = secrets.token_urlsafe(32)
            
            # Store session
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
        else:
            return {'error': 'Invalid email or password'}
    
    except Exception as e:
        print(f"❌ Authentication error: {e}")
        return {'error': str(e)}

def get_user_from_session(session_token):
    """Get user info from session token"""
    return sessions.get(session_token)

def save_order_to_salesforce(customer_id, session_id, cart_items, total, status='Draft'):
    """Save order to Salesforce with customer link"""
    if not sf:
        return None
    
    try:
        # Create Order record
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
        
        # Create Order Item records
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
            # Get order items
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

# AI Functions (same as before)
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

def extract_order_with_gemini(user_text, conversation_context=""):
    if is_greeting_or_casual(user_text):
        return []
    
    if not model:
        return fallback_extract_order(user_text)
    
    menu_items_list = list(MENU.keys())
    menu_text = ", ".join(menu_items_list)
    
    prompt = f"""Extract food items and quantities from this order.

Available items: {menu_text}

Customer said: "{user_text}"

Return ONLY valid JSON array:
[{{"item": "exact_menu_item", "quantity": number}}]

Examples:
"a burger" -> [{{"item": "burger", "quantity": 1}}]
"hello" -> []

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
            quantity = item.get('quantity', 1)
            if item_name in MENU:
                valid_items.append({
                    'key': item_name,
                    'name': MENU[item_name]['name'],
                    'price': MENU[item_name]['price'],
                    'quantity': quantity
                })
        return valid_items
    except Exception as e:
        print(f"Gemini extraction error: {e}")
        return fallback_extract_order(user_text)

def fallback_extract_order(user_text):
    items = []
    text_lower = user_text.lower()
    quantity_map = {
        'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5
    }
    for menu_key in MENU.keys():
        if menu_key in text_lower:
            quantity = 1
            pattern = r'(\w+)\s+' + menu_key
            match = re.search(pattern, text_lower)
            if match:
                qty_word = match.group(1)
                if qty_word in quantity_map:
                    quantity = quantity_map[qty_word]
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
        prompt = f"You're {ASSISTANT_NAME} at {RESTAURANT_NAME}. Write a 2-sentence welcome greeting{name_greeting}. Be casual."
    elif action == 'add' and added_items:
        items_text = ', '.join([f"{item['quantity']} {item['name']}" for item in added_items])
        prompt = f"Customer ordered: {items_text}. Total: ${total:.2f}. Confirm enthusiastically in 2 sentences."
    elif action == 'checkout':
        prompt = f"Customer checking out. Total: ${total:.2f}. Thank them warmly in 2 sentences."
    else:
        return "What can I get for you?"
    
    try:
        response = model.generate_content(prompt)
        return response.text.strip().strip('"\'')
    except:
        return get_fallback_response(action, added_items, total, user_name)

def get_fallback_response(action, added_items=None, total=0, user_name=''):
    name_greeting = f" {user_name}" if user_name else ""
    if action == 'welcome':
        return f"Hey{name_greeting}! Welcome to {RESTAURANT_NAME}! I'm {ASSISTANT_NAME}. What can I get you?"
    elif action == 'add' and added_items:
        items_text = ', '.join([f"{item['quantity']} {item['name']}" for item in added_items])
        return f"Got your {items_text}! That's ${total:.2f}. Want anything else?"
    elif action == 'checkout':
        return f"Thanks for ordering! Your total is ${total:.2f}. We'll have that ready soon!"
    return "What can I get for you?"

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
    """Register new user"""
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
    """Login user"""
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
    """Logout user"""
    data = request.json
    session_token = data.get('session_token')
    
    if session_token in sessions:
        del sessions[session_token]
    
    return jsonify({'success': True, 'message': 'Logged out successfully'})

@app.route('/api/auth/me', methods=['GET'])
def get_current_user():
    """Get current user info"""
    session_token = request.headers.get('Authorization', '').replace('Bearer ', '')
    
    user = get_user_from_session(session_token)
    
    if not user:
        return jsonify({'error': 'Not authenticated'}), 401
    
    return jsonify({'user': user})

@app.route('/api/orders/history', methods=['GET'])
def get_order_history():
    """Get user's order history"""
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
    """Process voice order (requires authentication)"""
    data = request.json
    user_text = data.get('text', '')
    session_id = data.get('session_id', 'default')
    session_token = data.get('session_token')
    
    # Get user from session
    user = get_user_from_session(session_token) if session_token else None
    customer_id = user['customer_id'] if user else None
    user_name = user['name'] if user else None
    
    print(f"Processing order - User: {user_name}, Said: '{user_text}'")
    
    if session_id not in carts:
        carts[session_id] = []
    if session_id not in conversation_history:
        conversation_history[session_id] = []
    
    conversation_history[session_id].append(f"Customer: {user_text}")
    lower_text = user_text.lower()
    
    # Greeting
    if is_greeting_or_casual(user_text):
        response_text = generate_response_with_gemini([], [], 0, action='welcome', user_name=user_name)
        return jsonify({
            'success': True,
            'cart': carts[session_id],
            'total': 0,
            'response': response_text,
            'items_added': []
        })
    
    # Checkout
    checkout_phrases = ['checkout', 'complete order', 'done', "that's all", 'finish']
    if any(phrase in lower_text for phrase in checkout_phrases):
        if not carts[session_id]:
            return jsonify({
                'success': False,
                'cart': [],
                'total': 0,
                'response': "Your cart's empty! What would you like?"
            })
        
        total = sum(item['price'] * item['quantity'] for item in carts[session_id])
        response_text = generate_response_with_gemini(carts[session_id], [], total, action='checkout', user_name=user_name)
        
        # Save to Salesforce with customer ID
        order_id = None
        if sf and customer_id:
            order_id = save_order_to_salesforce(customer_id, session_id, carts[session_id], total, 'Completed')
        
        return jsonify({
            'success': True,
            'cart': carts[session_id],
            'total': total,
            'response': response_text,
            'checkout': True,
            'order_id': order_id
        })
    
    # Extract items
    extracted_items = extract_order_with_gemini(user_text)
    
    if not extracted_items:
        return jsonify({
            'success': False,
            'cart': carts[session_id],
            'total': sum(item['price'] * item['quantity'] for item in carts[session_id]),
            'response': "Sorry, didn't catch that! What would you like?"
        })
    
    # Add to cart
    for item in extracted_items:
        existing = next((x for x in carts[session_id] if x['key'] == item['key']), None)
        if existing:
            existing['quantity'] += item['quantity']
        else:
            carts[session_id].append(item)
    
    total = sum(item['price'] * item['quantity'] for item in carts[session_id])
    response_text = generate_response_with_gemini(carts[session_id], extracted_items, total, action='add', user_name=user_name)
    
    # Save draft order
    if sf and customer_id:
        save_order_to_salesforce(customer_id, session_id, carts[session_id], total, 'Draft')
    
    return jsonify({
        'success': True,
        'cart': carts[session_id],
        'total': total,
        'response': response_text,
        'items_added': extracted_items
    })

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