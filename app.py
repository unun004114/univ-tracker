import os
import sqlite3
from flask import Flask, render_template, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from scraper import run_scraping_and_save, init_db, DB_PATH, URL_FILE_PATH, get_urls_from_file

app = Flask(__name__)

init_db()
# 10분마다 백그라운드 수집 실행
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(run_scraping_and_save, 'interval', minutes=10)
scheduler.start()

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/latest_all', methods=['GET'])
def get_latest_all():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT univ, type, dept, quota, apply, rate, timestamp
        FROM competition_history
        WHERE id IN (
            SELECT MAX(id) FROM competition_history GROUP BY univ, type, dept
        )
        ORDER BY rate DESC
    ''')
    rows = cursor.fetchall()
    conn.close()
    
    data = [{
        "univ": r[0], "type": r[1], "dept": r[2],
        "quota": r[3], "apply": r[4], "rate": r[5], "updated": r[6]
    } for r in rows]
    return jsonify({"data": data})

@app.route('/api/history', methods=['GET'])
def get_history():
    univ = request.args.get('univ')
    type_name = request.args.get('type')
    dept = request.args.get('dept')

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # 대학 + 전형 + 학과 3가지 조건으로 특정 항목의 시계열 추이 추출
    cursor.execute('''
        SELECT timestamp, rate, apply 
        FROM competition_history 
        WHERE univ = ? AND type = ? AND dept = ? 
        ORDER BY timestamp ASC
    ''', (univ, type_name, dept))
    rows = cursor.fetchall()
    conn.close()

    return jsonify({
        "timestamps": [r[0] for r in rows],
        "rates": [r[1] for r in rows],
        "applies": [r[2] for r in rows]
    })

@app.route('/api/urls', methods=['GET', 'POST'])
def handle_urls():
    if request.method == 'POST':
        content = request.json.get('content', '')
        with open(URL_FILE_PATH, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({"status": "success"})
    else:
        content = ""
        if os.path.exists(URL_FILE_PATH):
            with open(URL_FILE_PATH, 'r', encoding='utf-8') as f:
                content = f.read()
        return jsonify({"content": content})

@app.route('/api/trigger', methods=['POST'])
def trigger_now():
    run_scraping_and_save()
    return jsonify({"status": "success"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)