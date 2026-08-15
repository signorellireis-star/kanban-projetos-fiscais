import urllib.request
import urllib.error
import json
import datetime
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse

# Caminhos dos arquivos de configuração
CLICKUP_TOKEN_FILE = "ClickUp_TokenKey.md"
GEMINI_TOKEN_FILE = "GoogleGeminiToken.md"
CACHE_FILE = "status_cache.json"

LIST_ID = "187121803"

# Estado global do disjuntor de cota do Gemini (Circuit Breaker)
rate_limit_active = False

def read_token(filepath):
    if not os.path.exists(filepath):
        print(f"Erro: Arquivo {filepath} não encontrado.")
        return None
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read().strip()

# Inicializa tokens (lê primeiro das variáveis de ambiente para GitHub Actions, depois de arquivos locais)
CLICKUP_TOKEN = os.environ.get("CLICKUP_TOKEN") or read_token(CLICKUP_TOKEN_FILE)
GEMINI_TOKEN = os.environ.get("GEMINI_TOKEN") or read_token(GEMINI_TOKEN_FILE)

def get_url(url, token=CLICKUP_TOKEN):
    req = urllib.request.Request(url)
    req.add_header("Authorization", token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        print(f"Erro ao buscar URL {url}: {e}")
        return None

def format_date_golive(value):
    if not value:
        return ""
    try:
        ts = int(value) / 1000.0
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        meses = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
        return f"{meses[dt.month - 1]}/{dt.year}"
    except Exception as e:
        print(f"Erro ao formatar GoLive timestamp {value}: {e}")
        return ""

def format_date_prazo(value):
    if not value:
        return ""
    try:
        ts = int(value) / 1000.0
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        return f"{dt.month:02d}/{dt.year}"
    except Exception as e:
        print(f"Erro ao formatar prazo timestamp {value}: {e}")
        return ""

def get_dropdown_label(cf):
    val = cf.get("value")
    if val is None:
        return ""
    options = cf.get("type_config", {}).get("options", [])
    try:
        idx = int(val)
        if 0 <= idx < len(options):
            return options[idx].get("name", "")
    except:
        pass
    for opt in options:
        if str(opt.get("id")) == str(val) or str(opt.get("orderindex")) == str(val):
            return opt.get("name", "")
    return ""

def get_labels_value(cf):
    val = cf.get("value")
    if not val:
        return ""
    options = cf.get("type_config", {}).get("options", [])
    matched = []
    if isinstance(val, list):
        for opt_id in val:
            for opt in options:
                if opt.get("id") == opt_id:
                    matched.append(opt.get("label", ""))
    else:
        for opt in options:
            if opt.get("id") == val:
                matched.append(opt.get("label", ""))
    return matched[0] if matched else ""

def get_task_comments_text(task_id):
    """Busca o histórico de chat de uma tarefa no ClickUp"""
    url = f"https://api.clickup.com/api/v2/task/{task_id}/comment"
    data = get_url(url)
    if not data:
        return ""
    comments = data.get("comments", [])
    comments_text = []
    for c in reversed(comments):
        user = c.get("user", {}).get("username", "Usuário")
        text = c.get("comment_text", "").strip()
        if text:
            text = " ".join(text.split())
            comments_text.append(f"- {user}: {text}")
    return "\n".join(comments_text)

def get_gemini_summary(task_name, description, comments_text):
    """Chama a API do Gemini para resumir o último status do projeto com tratamento de limites de cota"""
    global rate_limit_active
    
    if not GEMINI_TOKEN:
        print("Aviso: Chave do Gemini não configurada. Pulando resumo de status por IA.")
        return ""
        
    if rate_limit_active:
        return ""
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={GEMINI_TOKEN}"
    
    prompt = f"""Você é um especialista em gestão de projetos.
Analise as informações do projeto '{task_name}' abaixo (descrição e histórico de mensagens do chat) e crie um resumo do ÚLTIMO STATUS DO PROJETO em apenas duas linhas, de forma profissional, gerencial e concisa.

Descrição do projeto:
{description if description else "(Sem descrição)"}

Histórico do chat/comentários (as mensagens mais recentes estão abaixo):
{comments_text if comments_text else "(Sem conversas registradas no chat)"}

Instruções importantes:
1. Responda estritamente em duas linhas, no máximo.
2. Não comece com "Resumo:", "O último status é:", "O projeto:", ou similares. Vá direto aos fatos.
3. Foque no andamento atual, impedimentos ou próximos passos citados.
4. Use linguagem formal e gerencial em português.
"""

    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }]
    }
    
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req) as response:
                res = json.loads(response.read().decode("utf-8"))
                text = res["candidates"][0]["content"]["parts"][0]["text"].strip()
                text = text.replace("Resumo:", "").replace("Último status:", "").replace("Ultimo status:", "").strip()
                return text
        except urllib.error.HTTPError as e:
            if e.code == 429:
                if attempt == 0:
                    sleep_time = 12
                    print(f"  [Aviso] Limite de cota atingido (429). Aguardando {sleep_time}s para tentar novamente...")
                    time.sleep(sleep_time)
                    continue
                else:
                    rate_limit_active = True
                    print("  [Corta-Circuito] Cota do Gemini excedida. Sincronização de IA pausada para as próximas tarefas.")
                    return ""
            else:
                try:
                    error_msg = e.read().decode("utf-8")
                except:
                    error_msg = str(e)
                print(f"Erro HTTP {e.code} ao chamar API do Gemini para '{task_name}': {error_msg}")
                return ""
        except Exception as e:
            print(f"Erro inesperado ao chamar API do Gemini para '{task_name}': {e}")
            return ""
            
    return ""

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Erro ao carregar cache: {e}")
    return {}

def save_cache(cache):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Erro ao salvar cache: {e}")

def load_current_raw_data():
    """Lê a lista de tarefas atual do arquivo data.js"""
    if not os.path.exists("data.js"):
        return []
    try:
        with open("data.js", "r", encoding="utf-8") as f:
            content = f.read()
        start = content.find("const rawData = ")
        if start == -1:
            return []
        start += len("const rawData = ")
        end = content.find("];", start)
        if end == -1:
            return []
        json_str = content[start:end+1]
        return json.loads(json_str)
    except Exception as e:
        print(f"Erro ao ler rawData de data.js: {e}")
        return []

def sync_single_task(task_id):
    """Sincroniza uma única tarefa do ClickUp de forma forçada"""
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    t = get_url(url)
    if not t:
        print(f"Erro ao buscar tarefa {task_id} no ClickUp.")
        return None
        
    t_name = t.get("name")
    clickup_status = t.get("status", {}).get("status", "").upper()
    date_updated = t.get("date_updated")
    custom_fields = t.get("custom_fields", [])
    cf_values = {cf.get("id"): cf for cf in custom_fields}
    
    # IDs dos campos personalizados
    CF_STATUS_AUXILIAR = "0f014407-1f9e-4407-b69f-d1dec49d4174"
    CF_GOLIVE = "d605644f-17f9-4a7d-9b08-36ae9752bc38"
    CF_PRAZO = "223957b9-527f-4817-9cd3-33ce72271414"
    CF_ANO_PPM = "cd499d6f-82d1-4cac-a833-0290598c81ce"
    CF_AREA = "573802da-a7a3-4b74-8763-b69b633cc51f"
    CF_RESUMO = "235d481b-c81a-41a7-80ce-ff9a7def00ff"
    CF_STATUS_PPM = "971e68a3-3d4b-40ad-8f73-791ec63b1e9a"
    
    # Mapeia Status Auxiliar
    kanban_status = ""
    status_aux_field = cf_values.get(CF_STATUS_AUXILIAR)
    if status_aux_field and status_aux_field.get("value") is not None:
        label = get_dropdown_label(status_aux_field)
        if label == "A FAZER":
            kanban_status = "A FAZER"
        elif label == "FAZENDO":
            kanban_status = "EM ANDAMENTO"
        elif label == "CONCLUIDO":
            kanban_status = "CONCLUIDO"
            
    if not kanban_status:
        if clickup_status == "CONCLUIDO":
            kanban_status = "CONCLUIDO"
        elif clickup_status in ["NOVO", "NAO PRIOR (RELEVANTE)", "PRIORIZADAS", "INICIAR DESENVOLVIMENTO"]:
            kanban_status = "A FAZER"
        else:
            kanban_status = "EM ANDAMENTO"
            
    # Mapeia datas e campos adicionais
    golive = format_date_golive(cf_values.get(CF_GOLIVE, {}).get("value"))
    prazo = format_date_prazo(cf_values.get(CF_PRAZO, {}).get("value"))
    ano_ppm = get_dropdown_label(cf_values.get(CF_ANO_PPM, {}))
    area = get_labels_value(cf_values.get(CF_AREA, {}))
    if not area:
        area = "Fiscal"
        
    status_ppm = get_dropdown_label(cf_values.get(CF_STATUS_PPM, {}))
    resumo_raw = cf_values.get(CF_RESUMO, {}).get("value", "")
    
    # Determina o resumo via Gemini (forçando regeneração)
    ultimo_status = ""
    status_updated_at = ""
    if kanban_status == "CONCLUIDO":
        ultimo_status = "Concluido"
    else:
        print(f"Solicitando novo resumo via Gemini para: {t_name}...")
        comments_text = get_task_comments_text(task_id)
        description = t.get("description", "")
        ultimo_status = get_gemini_summary(t_name, description, comments_text)
        
        if ultimo_status:
            status_updated_at = datetime.datetime.now().strftime('%d/%m/%Y %H:%M')
            # Atualiza o cache local
            cache = load_cache()
            cache[task_id] = {
                "date_updated": date_updated,
                "ultimo_status": ultimo_status,
                "status_updated_at": status_updated_at
            }
            save_cache(cache)
            
    if not ultimo_status:
        # Tenta pegar valor anterior do cache caso falhe
        cache = load_cache()
        cached = cache.get(task_id, {})
        ultimo_status = cached.get("ultimo_status", "")
        status_updated_at = cached.get("status_updated_at", "")
        
    task_mapped = {
        "id": task_id,
        "name": t_name,
        "status": clickup_status,
        "status_ppm": status_ppm,
        "kanban_status": kanban_status,
        "golive": golive,
        "prazo": prazo,
        "ano_ppm": ano_ppm,
        "area": area,
        "resumo_raw": resumo_raw,
        "ultimo_status": ultimo_status,
        "status_updated_at": status_updated_at
    }
    
    # Atualiza na lista do data.js
    raw_data_list = load_current_raw_data()
    found = False
    for i, item in enumerate(raw_data_list):
        if item.get("id") == task_id:
            raw_data_list[i] = task_mapped
            found = True
            break
    if not found:
        raw_data_list.append(task_mapped)
        
    # Escreve data.js
    resumos_map = {}
    for item in raw_data_list:
        r_raw = item.get("resumo_raw")
        if r_raw:
            resumos_map[item.get("name")] = r_raw
            
    now_str = datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    js_content = f"// Gerado via Sincronizador Local de API em {now_str}\n"
    js_content += f"const lastUpdate = '{now_str}';\n"
    js_content += f"const rawData = {json.dumps(raw_data_list, ensure_ascii=False, indent=2)};\n\n"
    js_content += f"const resumosMap = {json.dumps(resumos_map, ensure_ascii=False, indent=2)};\n"
    
    with open("data.js", "w", encoding="utf-8") as f:
        f.write(js_content)
        
    return task_mapped

class LocalSyncRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Desativa logs excessivos de requisições no console
        return

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        
    def do_GET(self):
        self.handle_request()
        
    def do_POST(self):
        self.handle_request()
        
    def handle_request(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/update-task':
            query = urllib.parse.parse_qs(parsed.query)
            task_id = query.get('id', [None])[0]
            if not task_id:
                self.send_error_response(400, "ID da tarefa ausente.")
                return
                
            task_updated = sync_single_task(task_id)
            if task_updated:
                self.send_json_response(200, task_updated)
            else:
                self.send_error_response(500, "Falha ao sincronizar tarefa.")
        elif parsed.path == '/sync-all':
            print("Solicitação de sincronização completa recebida do HTML...")
            try:
                # Chama a sincronização de dados completa sem reiniciar o servidor
                sync_data(start_server=False)
                self.send_json_response(200, {"success": True, "message": "Sincronização completa concluída com sucesso."})
            except Exception as e:
                print(f"Erro na sincronização completa via API: {e}")
                self.send_error_response(500, f"Erro ao sincronizar: {e}")
        else:
            self.send_error_response(404, "Rota não encontrada.")
            
    def send_json_response(self, status_code, data):
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
        
    def send_error_response(self, status_code, message):
        self.send_json_response(status_code, {"error": message})

def run_local_server():
    server_address = ('', 8000)
    try:
        httpd = HTTPServer(server_address, LocalSyncRequestHandler)
        print("----------------------------------------------------")
        print("Servidor de API local iniciado com sucesso!")
        print("Escutando em: http://localhost:8000")
        print("Mantenha esta janela aberta para atualizar projetos individuais no HTML.")
        print("----------------------------------------------------")
        httpd.serve_forever()
    except OSError:
        print("[Info] O servidor de API local já está rodando em outra janela (porta 8000 ocupada).")

def sync_data(start_server=True):
    if not CLICKUP_TOKEN:
        print("Erro: Chave de API do ClickUp não configurada no ClickUp_TokenKey.md.")
        return
        
    print("Iniciando sincronização completa de tarefas com ClickUp...")
    
    tasks = []
    page = 0
    while True:
        url = f"https://api.clickup.com/api/v2/list/{LIST_ID}/task?include_closed=true&limit=100&page={page}"
        print(f"Buscando página {page}...")
        data = get_url(url)
        if not data:
            break
        page_tasks = data.get("tasks", [])
        if not page_tasks:
            break
        tasks.extend(page_tasks)
        if len(page_tasks) < 100:
            break
        page += 1
        
    print(f"Total de tarefas recuperadas da API: {len(tasks)}")
    
    # Carrega cache
    cache = load_cache()
    
    filtered_tasks = []
    resumos_map = {}
    
    # IDs dos campos personalizados
    CF_LISTA_MURATA = "33fa38dc-1ecf-4f62-9496-2848a2ae9e4b"
    CF_STATUS_AUXILIAR = "0f014407-1f9e-4407-b69f-d1dec49d4174"
    CF_GOLIVE = "d605644f-17f9-4a7d-9b08-36ae9752bc38"
    CF_PRAZO = "223957b9-527f-4817-9cd3-33ce72271414"
    CF_ANO_PPM = "cd499d6f-82d1-4cac-a833-0290598c81ce"
    CF_AREA = "573802da-a7a3-4b74-8763-b69b633cc51f"
    CF_RESUMO = "235d481b-c81a-41a7-80ce-ff9a7def00ff"
    CF_STATUS_PPM = "971e68a3-3d4b-40ad-8f73-791ec63b1e9a"
    
    for idx, t in enumerate(tasks):
        is_murata = False
        custom_fields = t.get("custom_fields", [])
        cf_values = {cf.get("id"): cf for cf in custom_fields}
        
        murata_field = cf_values.get(CF_LISTA_MURATA)
        if murata_field and murata_field.get("value") is not None:
            val = get_dropdown_label(murata_field)
            if val.lower() == "murata":
                is_murata = True
                
        if not is_murata:
            continue
            
        t_id = t.get("id")
        t_name = t.get("name")
        clickup_status = t.get("status", {}).get("status", "").upper()
        date_updated = t.get("date_updated")
        
        # Mapeia Status Auxiliar
        kanban_status = ""
        status_aux_field = cf_values.get(CF_STATUS_AUXILIAR)
        if status_aux_field and status_aux_field.get("value") is not None:
            label = get_dropdown_label(status_aux_field)
            if label == "A FAZER":
                kanban_status = "A FAZER"
            elif label == "FAZENDO":
                kanban_status = "EM ANDAMENTO"
            elif label == "CONCLUIDO":
                kanban_status = "CONCLUIDO"
                
        if not kanban_status:
            if clickup_status == "CONCLUIDO":
                kanban_status = "CONCLUIDO"
            elif clickup_status in ["NOVO", "NAO PRIOR (RELEVANTE)", "PRIORIZADAS", "INICIAR DESENVOLVIMENTO"]:
                kanban_status = "A FAZER"
            else:
                kanban_status = "EM ANDAMENTO"
                
        golive = ""
        golive_field = cf_values.get(CF_GOLIVE)
        if golive_field and golive_field.get("value"):
            golive = format_date_golive(golive_field.get("value"))
            
        prazo = ""
        prazo_field = cf_values.get(CF_PRAZO)
        if prazo_field and prazo_field.get("value"):
            prazo = format_date_prazo(prazo_field.get("value"))
            
        ano_ppm = ""
        ano_ppm_field = cf_values.get(CF_ANO_PPM)
        if ano_ppm_field and ano_ppm_field.get("value") is not None:
            ano_ppm = get_dropdown_label(ano_ppm_field)
            
        area = ""
        area_field = cf_values.get(CF_AREA)
        if area_field and area_field.get("value"):
            area = get_labels_value(area_field)
        if not area:
            area = "Fiscal"
            
        status_ppm = ""
        status_ppm_field = cf_values.get(CF_STATUS_PPM)
        if status_ppm_field and status_ppm_field.get("value") is not None:
            status_ppm = get_dropdown_label(status_ppm_field)
            
        resumo_raw = ""
        resumo_field = cf_values.get(CF_RESUMO)
        if resumo_field and resumo_field.get("value"):
            resumo_raw = resumo_field.get("value")
            
        # Determina o último status usando cache
        ultimo_status = ""
        status_updated_at = ""
        if kanban_status == "CONCLUIDO":
            ultimo_status = "Concluido"
        else:
            cached_entry = cache.get(t_id)
            if cached_entry and cached_entry.get("date_updated") == date_updated and cached_entry.get("ultimo_status"):
                ultimo_status = cached_entry.get("ultimo_status", "")
                status_updated_at = cached_entry.get("status_updated_at", "")
                print(f"[{len(filtered_tasks) + 1}/30] Usando status em cache para: {t_name}")
            else:
                if rate_limit_active:
                    ultimo_status = ""
                    if cached_entry and cached_entry.get("ultimo_status"):
                        ultimo_status = cached_entry.get("ultimo_status")
                        status_updated_at = cached_entry.get("status_updated_at", "")
                        print(f"[{len(filtered_tasks) + 1}/30] Cota excedida, usando cache como fallback: {t_name}")
                    else:
                        print(f"[{len(filtered_tasks) + 1}/30] Cota excedida, resumo suspenso: {t_name}")
                else:
                    print(f"[{len(filtered_tasks) + 1}/30] Gerando resumo via Gemini para: {t_name}...")
                    comments_text = get_task_comments_text(t_id)
                    description = t.get("description", "")
                    ultimo_status = get_gemini_summary(t_name, description, comments_text)
                    
                    if ultimo_status:
                        status_updated_at = datetime.datetime.now().strftime('%d/%m/%Y %H:%M')
                        cache[t_id] = {
                            "date_updated": date_updated,
                            "ultimo_status": ultimo_status,
                            "status_updated_at": status_updated_at
                        }
                        save_cache(cache)
            
        task_mapped = {
            "id": t_id,
            "name": t_name,
            "status": clickup_status,
            "status_ppm": status_ppm,
            "kanban_status": kanban_status,
            "golive": golive,
            "prazo": prazo,
            "ano_ppm": ano_ppm,
            "area": area,
            "resumo_raw": resumo_raw,
            "ultimo_status": ultimo_status,
            "status_updated_at": status_updated_at
        }
        filtered_tasks.append(task_mapped)
        
        if resumo_raw:
            resumos_map[t_name] = resumo_raw
            
    print(f"Sincronização de tarefas concluída. Total de tarefas do Murata: {len(filtered_tasks)}")
    
    # Escreve o arquivo data.js na pasta local
    now_str = datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    js_content = f"// Gerado automaticamente via ClickUp API + Gemini em {now_str}\n"
    js_content += f"const lastUpdate = '{now_str}';\n"
    js_content += f"const rawData = {json.dumps(filtered_tasks, ensure_ascii=False, indent=2)};\n\n"
    js_content += f"const resumosMap = {json.dumps(resumos_map, ensure_ascii=False, indent=2)};\n"
    
    out_file = "data.js"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(js_content)
        
    print(f"Arquivo {out_file} gerado com sucesso!")
    
    if start_server:
        # Abre o navegador
        try:
            import webbrowser
            webbrowser.open("kanban_projetos_fiscais_light_dark.html")
        except Exception as e:
            print(f"Aviso: Não foi possível abrir o navegador automaticamente: {e}")
        
        # Inicia o servidor HTTP local
        run_local_server()

if __name__ == "__main__":
    import sys
    # Se rodar em ambiente de CI (GitHub Actions) ou com flag --no-server, roda apenas a sincronização
    is_ci = os.environ.get("CI") == "true" or "--no-server" in sys.argv or "--sync-only" in sys.argv
    sync_data(start_server=not is_ci)
