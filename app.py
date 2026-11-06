from flask import Flask, render_template, request, jsonify, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import sqlite3, os, secrets
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, 'bloodbridge.db')
app = Flask(__name__)
app.secret_key = os.environ.get('BLOODBRIDGE_SECRET', 'change-this-demo-secret-before-public-hosting')
GROUPS = ['A+', 'A-', 'B+', 'B-', 'O+', 'O-', 'AB+', 'AB-']

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; c.execute('PRAGMA foreign_keys=ON'); return c

def init_db():
    c = db(); q = c.cursor()
    q.execute('''CREATE TABLE IF NOT EXISTS facilities(id INTEGER PRIMARY KEY, name TEXT UNIQUE, distance REAL DEFAULT 0, updated_at TEXT)''')
    q.execute('''CREATE TABLE IF NOT EXISTS inventory(facility_id INTEGER, blood_group TEXT, units INTEGER NOT NULL, PRIMARY KEY(facility_id,blood_group), FOREIGN KEY(facility_id) REFERENCES facilities(id))''')
    q.execute('''CREATE TABLE IF NOT EXISTS facility_users(id INTEGER PRIMARY KEY AUTOINCREMENT, facility_id INTEGER UNIQUE NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, FOREIGN KEY(facility_id) REFERENCES facilities(id))''')
    q.execute('''CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT, blood_group TEXT, phone TEXT, facility_id INTEGER, status TEXT DEFAULT 'Pending', created_at TEXT, updated_at TEXT, FOREIGN KEY(facility_id) REFERENCES facilities(id))''')
    cols={r['name'] for r in q.execute('PRAGMA table_info(requests)').fetchall()}
    if 'requester_lat' not in cols: q.execute('ALTER TABLE requests ADD COLUMN requester_lat REAL')
    if 'requester_lon' not in cols: q.execute('ALTER TABLE requests ADD COLUMN requester_lon REAL')
    q.execute('CREATE TABLE IF NOT EXISTS donor_profiles(phone TEXT PRIMARY KEY, blood_group TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL, available INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL)')
    q.execute('CREATE TABLE IF NOT EXISTS donor_responses(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL, donor_phone TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(request_id,donor_phone), FOREIGN KEY(request_id) REFERENCES requests(id))')
    q.execute('''CREATE TABLE IF NOT EXISTS donor_state(id INTEGER PRIMARY KEY CHECK(id=1), available INTEGER NOT NULL DEFAULT 1)''')
    q.execute('INSERT OR IGNORE INTO donor_state(id,available) VALUES(1,1)')
    seeds = [
      ('CityCare Blood Bank',1.2,{'A+':12,'A-':5,'B+':8,'B-':3,'O+':10,'O-':2,'AB+':4,'AB-':1},8,'citycare@bloodbridge.demo','CityCare123'),
      ('Government General Hospital',2.6,{'A+':7,'A-':3,'B+':6,'B-':2,'O+':8,'O-':2,'AB+':3,'AB-':1},21,'ggh@bloodbridge.demo','Hospital123'),
      ('LifeLine Blood Centre',3.1,{'A+':4,'A-':2,'B+':3,'B-':1,'O+':5,'O-':1,'AB+':2,'AB-':1},43,'lifeline@bloodbridge.demo','LifeLine123')]
    now = datetime.now(timezone.utc)
    for name, dist, stock, mins, email, password in seeds:
        q.execute('INSERT OR IGNORE INTO facilities(name,distance,updated_at) VALUES(?,?,?)',(name,dist,(now-timedelta(minutes=mins)).isoformat()))
        fid = q.execute('SELECT id FROM facilities WHERE name=?',(name,)).fetchone()['id']
        for g,n in stock.items(): q.execute('INSERT OR IGNORE INTO inventory VALUES(?,?,?)',(fid,g,n))
        q.execute('INSERT OR IGNORE INTO facility_users(facility_id,email,password_hash) VALUES(?,?,?)',(fid,email,generate_password_hash(password)))
    c.commit(); c.close()

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('facility_id'): return jsonify(error='Please log in with your hospital or blood bank account.'),401
        return fn(*args, **kwargs)
    return wrapped

@app.route('/')
def home():
    # A fresh visit/reload must never restore a previous facility login.
    # The single-page navigation keeps working; this runs only when the
    # browser requests the site entry point again.
    session.clear()
    response = app.make_response(render_template('index.html'))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.get('/api/facilities')
def facilities():
    c=db(); rows=c.execute('''SELECT f.id,f.name,f.distance,f.updated_at,i.blood_group,i.units FROM facilities f JOIN inventory i ON f.id=i.facility_id ORDER BY f.distance''').fetchall(); c.close()
    out={}; now=datetime.now(timezone.utc)
    for r in rows:
        x=out.setdefault(r['id'],{'id':r['id'],'name':r['name'],'distance':r['distance'],'updated_at':r['updated_at'],'inventory':{}})
        x['inventory'][r['blood_group']]=r['units']
    for x in out.values():
        age=(now-datetime.fromisoformat(x['updated_at'])).total_seconds()/60
        x['age_minutes']=int(age); x['fresh']=age<=30
    return jsonify(list(out.values()))

@app.get('/api/me')
def me():
    if not session.get('facility_id'): return jsonify(logged_in=False)
    c=db(); f=c.execute('SELECT id,name FROM facilities WHERE id=?',(session['facility_id'],)).fetchone(); c.close()
    return jsonify(logged_in=bool(f), facility=dict(f) if f else None)

@app.post('/api/register')
def register():
    d=request.get_json(force=True); name=d.get('name','').strip(); email=d.get('email','').strip().lower(); password=d.get('password','')
    if len(name)<3 or '@' not in email or len(password)<8: return jsonify(error='Enter a facility name, valid email and password with at least 8 characters.'),400
    c=db()
    try:
        now=datetime.now(timezone.utc).isoformat()
        cur=c.execute('INSERT INTO facilities(name,distance,updated_at) VALUES(?,0,?)',(name,now)); fid=cur.lastrowid
        c.execute('INSERT INTO facility_users(facility_id,email,password_hash) VALUES(?,?,?)',(fid,email,generate_password_hash(password)))
        for g in GROUPS: c.execute('INSERT INTO inventory VALUES(?,?,0)',(fid,g))
        c.commit(); session['facility_id']=fid
    except sqlite3.IntegrityError:
        c.rollback(); c.close(); return jsonify(error='That facility name or email is already registered.'),409
    c.close(); return jsonify(message='Facility account created.',facility_id=fid)

@app.post('/api/login')
def login():
    d=request.get_json(force=True); email=d.get('email','').strip().lower(); password=d.get('password','')
    c=db(); u=c.execute('''SELECT u.*,f.name FROM facility_users u JOIN facilities f ON f.id=u.facility_id WHERE u.email=?''',(email,)).fetchone(); c.close()
    if not u or not check_password_hash(u['password_hash'],password): return jsonify(error='Incorrect email or password.'),401
    session.clear(); session['facility_id']=u['facility_id']; return jsonify(message='Logged in.',facility={'id':u['facility_id'],'name':u['name']})

@app.post('/api/logout')
def logout(): session.clear(); return jsonify(message='Logged out.')

@app.get('/api/my-facility')
@login_required
def my_facility():
    c=db(); f=c.execute('SELECT * FROM facilities WHERE id=?',(session['facility_id'],)).fetchone(); inv=c.execute('SELECT blood_group,units FROM inventory WHERE facility_id=?',(session['facility_id'],)).fetchall(); c.close()
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(f['updated_at'])).total_seconds()/60
    return jsonify(id=f['id'],name=f['name'],updated_at=f['updated_at'],age_minutes=int(age),fresh=age<=30,inventory={r['blood_group']:r['units'] for r in inv})

@app.get('/api/requests')
def get_requests():
    # Include the accepted donor's contact so the requester timeline can show it.
    c=db(); rows=c.execute('''
      SELECT r.*,f.name facility_name,
        (SELECT dr.status FROM donor_responses dr WHERE dr.request_id=r.id AND dr.status='Accepted' ORDER BY dr.updated_at DESC LIMIT 1) donor_status,
        (SELECT dr.donor_phone FROM donor_responses dr WHERE dr.request_id=r.id AND dr.status='Accepted' ORDER BY dr.updated_at DESC LIMIT 1) donor_phone
      FROM requests r JOIN facilities f ON f.id=r.facility_id ORDER BY r.id DESC
    ''').fetchall(); c.close(); return jsonify([dict(x) for x in rows])

@app.post('/api/requests')
def create_request():
    data=request.get_json(force=True); group=data.get('blood_group'); phone=data.get('phone','').strip(); ids=data.get('facility_ids',[])
    try: lat=float(data.get('latitude')); lon=float(data.get('longitude'))
    except (TypeError,ValueError): return jsonify(error='Share your current location so nearby donors can be notified.'),400
    if group not in GROUPS or not phone or len(phone)<10 or not ids: return jsonify(error='Enter a valid blood group, contact number and select at least one facility.'),400
    if not (-90<=lat<=90 and -180<=lon<=180): return jsonify(error='Invalid location coordinates.'),400
    batch=secrets.token_hex(10); now=datetime.now(timezone.utc).isoformat(); c=db(); created=[]
    for fid in set(ids):
        stock=c.execute('SELECT units FROM inventory WHERE facility_id=? AND blood_group=?',(fid,group)).fetchone()
        if stock and stock['units']>0:
            cur=c.execute('INSERT INTO requests(batch_id,blood_group,phone,facility_id,status,created_at,updated_at,requester_lat,requester_lon) VALUES(?,?,?,?,?,?,?,?,?)',(batch,group,phone,fid,'Pending',now,now,lat,lon)); created.append(cur.lastrowid)
    c.commit(); c.close()
    if not created: return jsonify(error='No selected facility currently shows available units for this blood group.'),409
    return jsonify(batch_id=batch,request_ids=created,message='Request sent to selected facilities.'),201

@app.post('/api/requests/<int:rid>/approve')
@login_required
def approve(rid):
    c=db(); c.execute('BEGIN IMMEDIATE'); r=c.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
    if not r or r['facility_id'] != session['facility_id']:
        c.rollback(); c.close(); return jsonify(error='This request does not belong to your facility.'),403
    if r['status']!='Pending': c.rollback(); c.close(); return jsonify(error='This request is no longer pending.'),409
    stock=c.execute('SELECT units FROM inventory WHERE facility_id=? AND blood_group=?',(r['facility_id'],r['blood_group'])).fetchone()
    if not stock or stock['units']<1: c.rollback(); c.close(); return jsonify(error='No stock available for this blood group.'),409
    c.execute('UPDATE inventory SET units=units-1 WHERE facility_id=? AND blood_group=?',(r['facility_id'],r['blood_group']))
    now=datetime.now(timezone.utc).isoformat(); c.execute("UPDATE requests SET status='Approved',updated_at=? WHERE id=?",(now,rid))
    c.execute("UPDATE requests SET status='Closed - fulfilled elsewhere',updated_at=? WHERE batch_id=? AND id<>? AND status='Pending'",(now,r['batch_id'],rid))
    c.execute('UPDATE facilities SET updated_at=? WHERE id=?',(now,r['facility_id']))
    c.commit(); c.close(); return jsonify(message='Approved; one unit reserved. Other pending copies were closed.')

@app.post('/api/my-facility/update')
@login_required
def update_stock():
    data=request.get_json(force=True); stock=data.get('inventory',{}); c=db()
    for g in GROUPS:
        try: n=max(0,int(stock.get(g,0)))
        except (TypeError,ValueError): n=0
        c.execute('INSERT INTO inventory(facility_id,blood_group,units) VALUES(?,?,?) ON CONFLICT(facility_id,blood_group) DO UPDATE SET units=excluded.units',(session['facility_id'],g,n))
    c.execute('UPDATE facilities SET updated_at=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),session['facility_id'])); c.commit(); c.close()
    return jsonify(message='Your facility inventory was saved.')

def distance_km(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, asin, sqrt
    dlat=radians(lat2-lat1); dlon=radians(lon2-lon1)
    a=sin(dlat/2)**2+cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    return 6371*2*asin(sqrt(a))

@app.post('/api/donor/profile')
def save_donor_profile():
    d=request.get_json(force=True); phone=d.get('phone','').strip(); group=d.get('blood_group')
    try: lat=float(d.get('latitude')); lon=float(d.get('longitude'))
    except (TypeError,ValueError): return jsonify(error='Allow location access to find requests near you.'),400
    if len(phone)!=10 or not phone.isdigit() or group not in GROUPS or not (-90<=lat<=90 and -180<=lon<=180): return jsonify(error='Enter a valid 10-digit phone number, blood group and share your location.'),400
    c=db(); now=datetime.now(timezone.utc).isoformat()
    c.execute("INSERT INTO donor_profiles(phone,blood_group,latitude,longitude,available,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(phone) DO UPDATE SET blood_group=excluded.blood_group,latitude=excluded.latitude,longitude=excluded.longitude,updated_at=excluded.updated_at",(phone,group,lat,lon,1,now)); c.commit(); c.close()
    return jsonify(message='Donor details saved.',phone=phone)

@app.post('/api/donor/availability')
def set_donor_availability():
    d=request.get_json(force=True); phone=d.get('phone','').strip(); val=1 if d.get('available') else 0; c=db()
    cur=c.execute('UPDATE donor_profiles SET available=?,updated_at=? WHERE phone=?',(val,datetime.now(timezone.utc).isoformat(),phone)); c.commit(); c.close()
    if not cur.rowcount: return jsonify(error='Save your phone number and location first.'),404
    return jsonify(available=bool(val))

def compatible_recipients(donor_group):
    # Red-cell donor compatibility: recipient groups that can receive this donor's blood.
    mapping={'O-':GROUPS,'O+':['O+','A+','B+','AB+'],'A-':['A-','A+','AB-','AB+'],'A+':['A+','AB+'],'B-':['B-','B+','AB-','AB+'],'B+':['B+','AB+'],'AB-':['AB-','AB+'],'AB+':['AB+']}
    return mapping.get(donor_group,[])

@app.get('/api/donor/requests')
def donor_requests():
    phone=request.args.get('phone','').strip(); c=db(); donor=c.execute('SELECT * FROM donor_profiles WHERE phone=?',(phone,)).fetchone()
    if not donor: c.close(); return jsonify(error='Enter your phone number and save your location first.'),400
    rows=c.execute("SELECT r.id,r.batch_id,r.blood_group,r.phone requester_phone,r.status,r.created_at,r.requester_lat,r.requester_lon,f.name facility_name,dr.status donor_status FROM requests r JOIN facilities f ON f.id=r.facility_id LEFT JOIN donor_responses dr ON dr.request_id=r.id AND dr.donor_phone=? WHERE r.status='Pending' AND r.requester_lat IS NOT NULL AND r.requester_lon IS NOT NULL AND (?=1 OR dr.status='Accepted') ORDER BY r.id DESC",(phone,donor['available'])).fetchall(); c.close(); out=[]; seen=set()
    for r in rows:
        if r['batch_id'] in seen: continue
        seen.add(r['batch_id']); dist=distance_km(donor['latitude'],donor['longitude'],r['requester_lat'],r['requester_lon'])
        if dist<=25 and r['blood_group'] in compatible_recipients(donor['blood_group']):
            item={'id':r['id'],'blood_group':r['blood_group'],'facility_name':r['facility_name'],'distance_km':round(dist,1),'created_at':r['created_at'],'donor_status':r['donor_status']}
            if r['donor_status']=='Accepted': item['requester_phone']=r['requester_phone']
            out.append(item)
    return jsonify(out)

@app.post('/api/donor/respond')
def donor_respond():
    d=request.get_json(force=True); phone=d.get('phone','').strip(); rid=int(d.get('request_id',0)); accept=bool(d.get('accept')); c=db()
    donor=c.execute('SELECT * FROM donor_profiles WHERE phone=?',(phone,)).fetchone(); r=c.execute("SELECT * FROM requests WHERE id=? AND status='Pending'",(rid,)).fetchone()
    if not donor: c.close(); return jsonify(error='Save your donor profile first.'),400
    if not r: c.close(); return jsonify(error='This request is no longer open.'),404
    dist=distance_km(donor['latitude'],donor['longitude'],r['requester_lat'],r['requester_lon'])
    if dist>25: c.close(); return jsonify(error='This request is outside your 25 km notification area.'),403
    status='Accepted' if accept else 'Declined'; now=datetime.now(timezone.utc).isoformat()
    c.execute("INSERT INTO donor_responses(request_id,donor_phone,status,updated_at) VALUES(?,?,?,?) ON CONFLICT(request_id,donor_phone) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at",(rid,phone,status,now)); c.commit(); c.close()
    return jsonify(status=status,message='Your response has been recorded.')

# Initialize the database for production WSGI servers such as Gunicorn.
init_db()

if __name__=='__main__':
    print('BloodBridge running at http://127.0.0.1:5000'); app.run(host='0.0.0.0',port=5000,debug=False)
