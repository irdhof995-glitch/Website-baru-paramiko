import os
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User, Device
from ssh_utils import SSHManager, run_batch_config
import pandas as pd
import io

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///network.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
socketio = SocketIO(app, cors_allowed_origins="*")

login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- Routes ---

@app.route('/')
@login_required
def dashboard():
    devices = Device.query.all()
    return render_template('dashboard.html', devices=devices)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Invalid username or password')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/devices/add', methods=['POST'])
@login_required
def add_device():
    hostname = request.form.get('hostname')
    ip = request.form.get('ip')
    username = request.form.get('username')
    password = request.form.get('password')
    
    new_device = Device(hostname=hostname, ip_address=ip, username=username, password=password)
    db.session.add(new_device)
    db.session.commit()
    flash('Device registered successfully')
    return redirect(url_for('dashboard'))

@app.route('/device/delete/<int:device_id>')
@login_required
def delete_device(device_id):
    device = Device.query.get_or_404(device_id)
    db.session.delete(device)
    db.session.commit()
    flash(f'Device {device.hostname} removed')
    return redirect(url_for('dashboard'))

@app.route('/device/update/<int:device_id>', methods=['POST'])
@login_required
def update_device(device_id):
    device = Device.query.get_or_404(device_id)
    device.hostname = request.form.get('hostname')
    device.ip_address = request.form.get('ip')
    device.username = request.form.get('username')
    new_password = request.form.get('password')
    if new_password:
        device.password = new_password
    db.session.commit()
    flash(f'Device {device.hostname} updated')
    return redirect(url_for('dashboard'))

@app.route('/device/<int:device_id>')
@login_required
def device_detail(device_id):
    device = Device.query.get_or_404(device_id)
    ssh = SSHManager(device.ip_address, device.username, device.password)
    success, msg = ssh.connect()
    
    if not success:
        return render_template('device_detail.html', device=device, error=msg)
    
    info = ssh.get_router_info()
    shell_output, shell_err = ssh.check_interfaces_shell()
    
    if shell_err:
        interfaces = ssh.parse_interfaces(info.get('interfaces', ''))
    else:
        # Use shell output for parsing if available
        interfaces = ssh.parse_interfaces(shell_output)
        
    ssh.close()
    
    return render_template('device_detail.html', device=device, info=info, interfaces=interfaces)

@app.route('/device/<int:device_id>/configure_ip', methods=['POST'])
@login_required
def configure_ip(device_id):
    device = Device.query.get_or_404(device_id)
    interface = request.form.get('interface')
    ip = request.form.get('ip')
    mask = request.form.get('mask')
    
    ssh = SSHManager(device.ip_address, device.username, device.password)
    ssh.connect()
    success, output = ssh.configure_ip(interface, ip, mask)
    ssh.close()
    
    if success:
        flash(f'Successfully configured {interface}')
    else:
        flash(f'Failed to configure {interface}: {output}')
    
    return redirect(url_for('device_detail', device_id=device_id))

@app.route('/batch', methods=['GET', 'POST'])
@login_required
def batch_config():
    if request.method == 'POST':
        device_ids = request.form.getlist('devices')
        config_text = request.form.get('config_text')
        raw_commands = request.form.get('raw_commands')
        csv_file = request.files.get('csv_file')
        
        results = []
        commands = []
        
        if raw_commands:
            commands = [c.strip() for c in raw_commands.split('\n') if c.strip()]
        elif config_text:
            # Simple parser for "R1, Gi0/1, add, 10.1.1.1/24"
            lines = [l.strip() for l in config_text.split('\n') if l.strip()]
            for line in lines:
                parts = [p.strip() for p in line.split(',')]
                if len(parts) >= 4:
                    # Target specific device if hostname matches, or just add to general list
                    commands.append(f"interface {parts[1]}")
                    commands.append(f"ip address {parts[3].replace('/', ' ')}")
                    commands.append("no shutdown")
        
        # If no specific devices selected but mentioned in config_text, we'd need more complex logic.
        # For now, apply commands to all selected checkboxes.
        if not device_ids:
            devices = Device.query.all()
            device_ids = [str(d.id) for d in devices]

        for d_id in device_ids:
            device = Device.query.get(d_id)
            if device:
                success, output = run_batch_config({
                    'ip': device.ip_address,
                    'username': device.username,
                    'password': device.password
                }, commands)
                results.append({'hostname': device.hostname, 'success': success, 'output': output})
            
        return render_template('batch_results.html', results=results)
    
    devices = Device.query.all()
    return render_template('batch_config.html', devices=devices)

# --- User Management ---

@app.route('/users')
@login_required
def users():
    users_list = User.query.all()
    return render_template('users.html', users=users_list)

@app.route('/users/add', methods=['POST'])
@login_required
def add_user():
    username = request.form.get('username')
    password = request.form.get('password')
    
    if User.query.filter_by(username=username).first():
        flash('Username already exists')
    else:
        new_user = User(username=username, password=generate_password_hash(password))
        db.session.add(new_user)
        db.session.commit()
        flash('User created successfully')
    return redirect(url_for('users'))

@app.route('/users/delete/<int:user_id>')
@login_required
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.username == 'admin':
        flash('Cannot delete default admin')
    else:
        db.session.delete(user)
        db.session.commit()
        flash('User removed')
    return redirect(url_for('users'))

@app.route('/interfaces')
@login_required
def all_interfaces():
    devices = Device.query.all()
    return render_template('interfaces.html', devices=devices)

@app.route('/logs')
@login_required
def system_logs():
    return render_template('logs.html')

@app.route('/terminal')
@login_required
def terminal():
    devices = Device.query.all()
    return render_template('terminal.html', devices=devices)

# --- Terminal (SocketIO) ---

active_shells = {}

@socketio.on('connect_terminal')
def handle_terminal_connect(data):
    device_id = data.get('device_id')
    sid = request.sid
    device = Device.query.get(device_id)
    
    if not device:
        emit('terminal_output', {'data': 'Device not found\n'})
        return

    ssh = SSHManager(device.ip_address, device.username, device.password)
    success, msg = ssh.connect()
    
    if not success:
        emit('terminal_output', {'data': f'Connection failed: {msg}\n'})
        return

    shell = ssh.client.invoke_shell()
    active_shells[sid] = {'shell': shell, 'client': ssh.client}
    
    def background_thread(session_id):
        while session_id in active_shells:
            sh = active_shells[session_id]['shell']
            if sh.recv_ready():
                try:
                    output = sh.recv(1024).decode('utf-8', errors='ignore')
                    socketio.emit('terminal_output', {'data': output}, room=session_id)
                except:
                    break
            socketio.sleep(0.1)

    socketio.start_background_task(background_thread, sid)

@socketio.on('terminal_input')
def handle_terminal_input(data):
    sid = request.sid
    input_text = data.get('data')
    if sid in active_shells:
        try:
            active_shells[sid]['shell'].send(input_text)
        except:
            pass

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    if sid in active_shells:
        try:
            active_shells[sid]['shell'].close()
            active_shells[sid]['client'].close()
        except:
            pass
        del active_shells[sid]

# --- Init Database ---

@app.cli.command("init-db")
def init_db():
    db.create_all()
    # Create default admin
    if not User.query.filter_by(username='admin').first():
        admin = User(username='admin', password=generate_password_hash('admin123'))
        db.session.add(admin)
        db.session.commit()
    print("Database initialized.")

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
