import os
from pathlib import Path
import re
import requests
import pandas as pd
from typing import List, Dict, Union
from functools import wraps
from flask import Flask, render_template, abort, url_for,request,jsonify,redirect

import markdown
from bs4 import BeautifulSoup
import json


from openai import OpenAI
#from dotenv import load_dotenv

from dotenv import dotenv_values

# 1. 直接读取 .env → dict，不注入环境变量
cfg =dotenv_values(".env")       # 自动取当前目录下的 .env


app = Flask(__name__)

# 定义 Markdown 存放的根目录
# 所有的 .md 文件应该存放在这个 ./md 目录下
MD_ROOT = 'md'

SAVE_DIR = os.path.join(os.getcwd(), MD_ROOT)
os.makedirs(SAVE_DIR, exist_ok=True)


# 配置
MD_ROOT = './md'  # Markdown文件存储目录
SAVE_DIR = './md'  # 与MD_ROOT相同，用于保存文件

# 确保目录存在
os.makedirs(MD_ROOT, exist_ok=True)
os.makedirs(SAVE_DIR, exist_ok=True)

# ------------------------------------------------------------ 辅助函数 ---------------------------------------------

def get_setcode_from_code(code: str) -> int:
    """
    根据股票代码前缀判断 Setcode (0: 深市, 1: 沪市, 2: 北交所)
    """
    code_str = str(code)
    
    if len(code_str) != 6:
        return -1 
        
    prefix = code_str[:3]
    
    if prefix.startswith('60') or prefix.startswith('68'):
        # 60x, 68x (上交所主板/科创板)
        return 1
    elif prefix.startswith('00') or prefix.startswith('30'):
        # 00x, 30x (深交所主板/创业板)
        return 0
    elif prefix.startswith('8') or prefix.startswith('9'):
        # 8xx, 9xx (北交所)
        return 2
    else:
        return -1

def get_setcode_from_code_dfcf(code: str) -> int:
    """
    根据股票代码前缀判断 Setcode (0: 深市, 1: 沪市, 2: 北交所)
    """
    code_str = str(code)
    
    if len(code_str) != 6:
        return -1 
        
    prefix = code_str[:3]
    
    if prefix.startswith('60') or prefix.startswith('68'):
        # 60x, 68x (上交所主板/科创板)
        return 1
    elif prefix.startswith('00') or prefix.startswith('30'):
        # 00x, 30x (深交所主板/创业板)
        return 2
    elif prefix.startswith('8') or prefix.startswith('9'):
        # 8xx, 9xx (北交所)
        return 3
    else:
        return -1
# --- 数据获取函数 ---

def query_icfqs(secu_list: List[Dict[str, Union[str, int]]]) -> pd.DataFrame:
    """
    调用 icfqs 实时接口，返回指定股票快照
    """
    # 实际地址和参数保持不变
    url = "http://hot.icfqs.com:7615/TQLEX?Entry=HQServ.CombHQ"
    headers = {
        "Accept": "text/plain, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "http://hot.icfqs.com:7615/site/pcwebcall/html/pc_zttzk_zttzk_gn.html",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }

    if not secu_list:
        return pd.DataFrame()

    payload = {
        # WantCol 必须包含 ACTIVECAPITAL, CLOSE, NOW
        "WantCol": ["ACTIVECAPITAL", "CLOSE", "NOW"], 
        "Secu": secu_list
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        
        if not data.get("List"):
            return pd.DataFrame(columns=data.get("ListHead", []))

        df = pd.DataFrame(data["List"], columns=data["ListHead"])
        
        # 数据类型转换和格式化
        df["Code"] = df["Code"].astype(str).str.zfill(6) 
        df["Setcode"] = df["Setcode"].astype(int)
        df["CLOSE"] = df["CLOSE"].astype(float)
        df["NOW"] = df["NOW"].astype(float)
        df["ACTIVECAPITAL"] = df["ACTIVECAPITAL"].astype(float) 
        
        return df
    except Exception as e:
        print(f"Error querying stock data: {e}")
        return pd.DataFrame()


# --- 核心装饰器 (股票数据注入) ---

def add_stock_changes(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # f(*args, **kwargs) 在这里是调用 serve_md 函数，获取其返回的 HTML 字符串
        env = kwargs.get('env', 'tdx')
        url_prefix = request.view_args.get('url_prefix', "http://www.treeid/breed_1")
        html_content = f(*args, **kwargs)
        
        # 使用 BeautifulSoup 解析 HTML 内容，以便只在文档正文中搜索股票代码，排除脚本等
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 假设我们只关心 body 内的文本
        body_text = soup.find('body').get_text() if soup.find('body') else html_content
        
        # 查找所有 6 位数字代码
        all_codes = re.findall(r'\b\d{6}\b', body_text)
        
        if not all_codes:
            return html_content

        unique_codes = sorted(list(set(all_codes)))
        query_list = []
        
        for code_str in unique_codes:
            setcode = get_setcode_from_code(code_str)
            if setcode != -1:
                # Code 保持为字符串
                query_list.append({"Code": code_str, "Setcode": setcode})

        # 批量查询数据
        df = query_icfqs(query_list)
        
        replace_dict = {}
        if not df.empty:
            for _, row in df.iterrows():
                code_str = str(row['Code']) 
                close = row['CLOSE']
                now = row['NOW']
                active_capital = row['ACTIVECAPITAL']

                pct_str = ""
                now_str = f"{now:.2f}"
                # 将流动市值 (亿元) 格式化
                active_capital_str = f" {active_capital*now/10000:.0f}亿"

                color = "black"
                now_color = "black"

                # 涨跌幅计算和颜色判断
                if now > 0 and close > 0:
                    pct_change = ((now - close) / close) * 100
                    
                    symbol = "+" if pct_change > 0 else ""
                    pct_str = f"{symbol}{pct_change:.2f}%"
                    
                    if pct_change > 0:
                        color = "red"
                    elif pct_change < 0:
                        color = "green"
                    now_color = color # 现价颜色与涨跌幅一致
                
                if env == "tdx":
                    code_href = f'<a href="{url_prefix}{code_str}" style="color: black;">{code_str}</a>'
                
                elif env == "dfcf":
                    market_code = get_setcode_from_code_dfcf(code_str)
                    market_code = str(market_code)
                    code_href = f'<a href="https://quote.eastmoney.com/basic/h5chart-iframe.html?code={code_str}&market={market_code}&type=k" style="color: black;" target="_blank">{code}</a>'
                else:
                    code_href = ''
                # 构建注入的 HTML 片段 (包含现价、涨跌幅、流动市值)
                format_html = f"""{code_href} <span style="color: {now_color}; font-weight: bold;">{now_str}</span><span style="color: {color}; margin-left: 8px; font-weight: bold;">({pct_str})</span><span style="color: #007bff; margin-left: 8px;">[{active_capital_str}]</span>"""
                replace_dict[code_str] = format_html
        
        # 执行正则表达式替换
        def replacer(match):
            code = match.group(0)
            
            # 只替换不在其他数字（如年份、日期）附近的独立 6 位数字
            if code in replace_dict:
                return replace_dict[code] 
            else:
                return code

        # 使用正则表达式在文档主体内容中替换，不影响 HTML 标签
        # 注意: 这里直接对整个 HTML 字符串进行替换，可能会替换到 HTML 属性或注释中的 6 位数字，
        # 但对于 Markdown 渲染后的简单结构，风险可控。
        modified_html = re.sub(r'\b\d{6}\b', replacer, html_content)

        return modified_html
        
    return decorated_function



# ------------------------------------------------------------ 路由定义 ---------------------------------------------



@app.route('/')
def index():
    return redirect(url_for('chat'))     # 302 默认


from html import escape
@app.route('/md/<keyword>')
@add_stock_changes 
def serve_md(keyword):
    """
    处理 /md/<keyword> 请求，读取 Markdown 文件，渲染成 HTML，并注入股票数据。
    注意：参数名已统一为 keyword。
    """
    # 路径： ./md/keyword.md
    file_path = os.path.join(MD_ROOT, f"{keyword}.md")
    env = request.args.get('env', 'web')
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            md_content = f.read()
    except FileNotFoundError:
        abort(404, description=f"文件 '{keyword}.md' 未找到。")
    except Exception as e:
        abort(500, description=f"读取文件时发生错误: {e}")

    # 使用 extensions=['tables'] 支持 Markdown 表格
    html_body = markdown.markdown(md_content, extensions=['tables'])
    html_body = f'<div style="white-space: pre-line">{escape(md_content)}</div>'
    # 包装成带 GitHub 风格 CSS 的完整 HTML 页面
    # 在 serve_md 函数里，把原来的 full_html 换成下面这段：
    full_html = f"""
<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <title>{keyword.replace('_', ' ').title()}</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.2.0/github-markdown.min.css">
    <style>
        .markdown-body{{box-sizing:border-box;min-width:200px;max-width:800px;margin:0 auto;padding:40px}}
        .stock-info{{display:inline-block}}
        .markdown-body li{{list-style:disc;margin-left:20px}}
        .markdown-body pre{{background:#f6f8fa;border:1px solid #c9d1d9;border-radius:6px;padding:16px;overflow-x:auto}}
        /* 股票折叠区域 */
        #stockBox{{height:90vh;border-top:1px solid #ddd;background:#fff;display:block;position:relative}}
        #stockBox iframe{{width:100%;height:calc(100% - 50px);border:none}}
        #toggleStock{{position:absolute;top:6px;right:10px;z-index:10;padding:4px 10px;background:#071b39;color:#fff;border:none;border-radius:4px;font-size:12px;cursor:pointer}}
    </style>
</head>
<body>
    <div class="markdown-body">{html_body}</div>

    <!-- 股票详情 -->
    <div id="stockBox">
        <button id="toggleStock">展开</button>
        <iframe id="stockFrame" src="" style="display:none"></iframe>
    </div>

    <script>
    (function(){{
        const stockBox   = document.getElementById('stockBox');
        const stockFrame = document.getElementById('stockFrame');
        const toggleBtn  = document.getElementById('toggleStock');
        let expanded = false;

        /* 只在 .markdown-body 里找股票代码 */
        function getCodes(){{
            const mb = document.querySelector('.markdown-body');
            if (!mb) return [];
            // 修正正则表达式：匹配6位数字，以0、3、6开头
            const m = mb.textContent.match(/\\b[036]\\d{{5}}\\b/g);
            return m ? [...new Set(m)] : [];
        }}

        /* 写入 iframe */
        function fillIframe(){{
            const codes = getCodes();
            console.log('找到股票代码:', codes); // 调试用
            if (codes.length) {{
                // 协议自动跟随当前页面（避免 mixed-content）
                const src = '/stocklist?env={env}&code_list=' +
                             encodeURIComponent(codes.join('|'));
                stockFrame.src = src;
                stockBox.style.display = 'block';
            }} else {{
                stockBox.style.display = 'none';
                console.log('未找到股票代码');
            }}
        }}

        /* DOM 就绪后执行 */
        if (document.readyState === 'loading') {{
            document.addEventListener('DOMContentLoaded', fillIframe);
        }} else {{
            fillIframe();          // 已被缓存，直接跑
        }}

        /* 展开 / 收起 */
        toggleBtn.onclick = () => {{
            expanded = !expanded;
            if (expanded) {{
                stockBox.style.height = '90vh';
                stockFrame.style.display = 'block';
                toggleBtn.textContent = '收起';
            }} else {{
                stockBox.style.height = '50px';
                stockFrame.style.display = 'none';
                toggleBtn.textContent = '展开';
            }}
        }};
    }})();                             // 立即执行
    </script>
</body>
</html>
"""
    return full_html 


def get_markdown_files():
    """扫描 MD_ROOT 目录，获取所有 .md 文件的文件名（不含扩展名）作为关键词。"""
    keywords = []
    # 确保目录存在
    if not os.path.exists(MD_ROOT):
        os.makedirs(MD_ROOT) # 如果目录不存在，则创建它
        print(f"Info: Markdown directory {MD_ROOT} created.")

    for filename in os.listdir(MD_ROOT):
        if filename.endswith('.md'):
            # 使用文件名（不带扩展名）作为关键词
            keyword = os.path.splitext(filename)[0]
            keywords.append(keyword)
    return keywords


@app.route('/list')
def list_md():
    env = request.args.get('env', 'dfcf')
    """主页路由：扫描文件并渲染包含关键词列表和 iframe 的模板。"""
    keywords = get_markdown_files()
    
    # 默认加载第一个关键词的文档
    initial_keyword = keywords[0] if keywords else ""
    # index.html 模板将接收实时获取的关键词列表
    # 注意: initial_keyword 在模板中调用 url_for('serve_md', keyword=...)
    return render_template('list.html', keywords=keywords, env=env,initial_keyword=initial_keyword)

##################################
from typing import List, Dict, Union
from datetime import datetime
def get_setcode_from_code_tdx(code: str) -> int:
    """
    根据股票代码前缀判断 Setcode (0: 深市, 1: 沪市, 2: 北交所)
    """
    code_str = str(code)
    
    if len(code_str) != 6:
        return -1 
        
    prefix = code_str[:3]
    
    if prefix.startswith('60') or prefix.startswith('68'):
        # 60x, 68x (上交所主板/科创板)
        return 1
    elif prefix.startswith('00') or prefix.startswith('30'):
        # 00x, 30x (深交所主板/创业板)
        return 0
    elif prefix.startswith('8') or prefix.startswith('9'):
        # 8xx, 9xx (北交所)
        return 2
    else:
        return -1


def query_icfqs(secu_list: List[Dict[str, Union[str, int]]]) -> pd.DataFrame:
    """
    调用 icfqs 实时接口，返回指定股票快照
    """
    # 实际地址和参数保持不变
    url = "http://hot.icfqs.com:7615/TQLEX?Entry=HQServ.CombHQ"
    headers = {
        "Accept": "text/plain, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "http://hot.icfqs.com:7615/site/pcwebcall/html/pc_zttzk_zttzk_gn.html",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }

    if not secu_list:
        return pd.DataFrame()

    payload = {
        # WantCol 必须包含 ACTIVECAPITAL, CLOSE, NOW
        "WantCol": ["ACTIVECAPITAL", "CLOSE", "NOW"], 
        "Secu": secu_list
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        
        if not data.get("List"):
            return pd.DataFrame(columns=data.get("ListHead", []))

        df = pd.DataFrame(data["List"], columns=data["ListHead"])
        
        # 数据类型转换和格式化
        df["Code"] = df["Code"].astype(str).str.zfill(6) 
        df["Setcode"] = df["Setcode"].astype(int)
        df["CLOSE"] = df["CLOSE"].astype(float).round(2)
        df["NOW"] = df["NOW"].astype(float).round(2)
        df["ACTIVECAPITAL"] = df["ACTIVECAPITAL"].astype(float) 
        df['pct_change'] = ((df['NOW'] - df['CLOSE'] ) / df['CLOSE'] * 100).round(2)
        
        return df
    except Exception as e:
        print(f"Error querying stock data: {e}")
        return pd.DataFrame()

@app.route("/stocklist", methods=["GET"])
def stocklist():
    code_list = request.args.get('code_list','')  # 限定股票域
    df = pd.read_csv("./data/通达信A股信息.txt",encoding='gbk',sep='\t',dtype=str)   #  更新 股票列表
    df = df[:-1]
    df = df[['代码','名称','一二级行业','细分行业','主营构成']]                # 使用所有数据
    df['一级行业']=df['一二级行业'].astype('str').fillna('-')
    df['一级行业']=df['一二级行业'].astype('str').apply(lambda x:x.split("-")[0])
    df['二级行业']=df['一二级行业'].astype('str').apply(lambda x:x.split("-")[1] if '-' in x else '')
    df=df.rename(columns={"细分行业":"三级行业"})
    df['名称'] = df.apply(lambda row:f'<a href="http://www.treeid/breed_1{row["代码"]}" style="color: black;text-decoration: none;">{row["名称"]}</a>', axis=1)
    if code_list:
        code_list = code_list.split('|')
        df = df[df['代码'].apply(lambda x:x in code_list)].reset_index(drop=True)
   #------------------------------------   更新实时行情  ---------------------
    
    all_codes = df['代码'].to_list()
    unique_codes = sorted(list(set(all_codes)))

    query_list = []
    df =df.head(500)
    for code_str in unique_codes:
        code_str = str(code_str)
        setcode = get_setcode_from_code_tdx(code_str)   
        if setcode != -1:
            query_list.append({"Code": code_str, "Setcode": setcode})
    # ---------- ----------
    def chunked(seq, size=60):
        """把序列 seq 按 size 长度切片"""
        for i in range(0, len(seq), size):
            yield seq[i:i + size]
    # ---------- ----
    df_live_list = []
    for sub_q in chunked(query_list, 60):          # 每批最多 60 个
        df_live_list.append(query_icfqs(sub_q))    # 逐批调用

    df_live = pd.concat(df_live_list, ignore_index=True)  # 合并成总表            
    df['现价'] = df['代码'].map(df_live.set_index('Code')['NOW'])
    df['涨幅'] = df['代码'].map(df_live.set_index('Code')['pct_change'])
    df['流通市值'] = df['代码'].map(df_live.set_index('Code')['ACTIVECAPITAL'])
    df['流通市值'] = (df['流通市值'] *df['现价']/10000).round(2)
    #---------------------------------------------  数据过滤完毕  下一步格式化，展示 ------------------------------


    df['涨幅'] = df.apply(lambda row: f'<a  style=" border-radius: 10px;background-color: {"red;color:black;" if row["涨幅"] > 9.85 else ("Salmon;color:black;" if row["涨幅"] > 0 else "green;color:black;")};">{row["涨幅"]}</a>', axis=1)

# ------------------------------   表格 --------------------------------
    cols = ['代码','名称','现价','涨幅','流通市值','一级行业','二级行业','三级行业','主营构成']
    df = df[cols]
    if len(df)==0:
        return '列表为空'
    else:
        data_for_datatables = df.to_dict(orient='records')
        # 生成 HTML 表格
        html_table = f"""
        <style>
             /* 修改 DataTables 横向滚动条下方的线条颜色 */
            #myTable_wrapper .dataTables_scrollBody {{
            border-bottom: 1px solid #d3d3d3;  /* 修改为灰色 */
           
        }}
        /* 搜索框靠右 + 宽度 10% */
        .dataTables_filter {{
            float: right;
            width: 8%;
        }}
        .dataTables_filter input {{
            width: 100%;   /* 让输入框撑满这 10% */
            box-sizing: border-box;
        }}
        
        /* 1. 表头居中 */
    #myTable thead th {{
        text-align: center;      /* 水平居中 */
        vertical-align: middle;  /* 垂直居中 */
    }}

    /* 2. 单元格居中 */
    #myTable tbody td {{
        text-align: center;
        vertical-align: middle;
    }}
        </style>
        
        <link rel="stylesheet" type="text/css" href="https://cdn.datatables.net/1.10.25/css/jquery.dataTables.css">
        <script type="text/javascript" charset="utf8" src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
        <script type="text/javascript" charset="utf8" src="https://cdn.datatables.net/1.10.25/js/jquery.dataTables.js"></script>

        <link rel="stylesheet" type="text/css" href="https://cdn.datatables.net/select/1.3.1/css/select.dataTables.min.css">
        <script type="text/javascript" charset="utf8" src="https://cdn.datatables.net/select/1.3.1/js/dataTables.select.min.js"></script>

        <table id="myTable" class="display" style="top: 35px;width:100%;white-space: nowrap;font-size: 14px;">
            <thead>
                <tr>
                    {''.join([f"<th>{col}</th>" for col in df.columns])}
                </tr>
            </thead>
            <tbody >
                {"".join([f"<tr style='cursor: pointer;'>{''.join([f'<td>{row[col]}</td>' for col in df.columns])}</tr>" for row in data_for_datatables])}
            </tbody>
        </table>
        
        <script>
            // 初始化 DataTables 插件，并启用筛选功能
            $(document).ready(function() {{
                var table = $('#myTable').DataTable({{
                    "scrollY": true, // 固定表头需要设置高度
                    "paging": true, // 
                    "pageLength": 25, 
                    language: {{ search: '' }}, // 去掉默认的“Search:”文字
                    "order": [],
                    "scrollX": true,
                    "scrollCollapse": true,
                     "bFilter": true, //过滤功能
                    //"select": true,
                     "select": {{ style: 'single' }}, // 启用单选功能
                    "autoWidth": true, // 禁止自动计算列宽
                    //"columnDefs": [{{ "width": "20%", "targets": 0 }}], // 自定义列宽，防止错位
                }});

                                 // 初始化默认选中第一行
                table.row(0).select();
        
                // 监听键盘上下箭头事件
                $(document).keydown(function(e) {{
                    var selectedRow = table.row('.selected');
                    var rowIndex = selectedRow.index();  // 获取当前选中行的索引
                    var newRowIndex;
        
                    if (e.key === 'ArrowUp') {{
                        newRowIndex = rowIndex - 1; // 上一行
                    }} else if (e.key === 'ArrowDown') {{
                        newRowIndex = rowIndex + 1; // 下一行
                    }} else {{
                        return; // 不处理其他键
                    }}
        
                    if (newRowIndex >= 0 && newRowIndex < table.rows().count()) {{
                        table.rows().deselect();
                        table.row(newRowIndex).select();
        
                        // 自动触发第三列的超链接点击事件
                        var rowData = table.row(newRowIndex).node();
                        var link = $(rowData).find('td:eq(2) a'); // 获取第二列中的链接
                        if (link.length) {{
                            link[0].click();  // 模拟点击事件
                            table.row(newRowIndex).select();
                        }}
                    }}
                }});
  

            // 为表格行添加事件 - 单击
            $('#myTable tbody').on('click', 'tr', function() {{
                    var selectedRow = table.row('.selected');
                    
                    var rowIndex = selectedRow.index();  // 获取当前选中行的索引
                        // 自动触发第二列的超链接点击事件
                        var rowData = table.row(this).node();
                        var link = $(rowData).find('td:eq(2) a'); // 获取第二列中的链接
                        if (link.length ) {{
                            link[0].click();  // 模拟点击事件
                         table.row(rowIndex).select();
                        }}
                }});
               
            }});
            
        </script>

        """


    # 添加控件
    html_option = f'''
    <script>
   // 为输入框添加回车键事件监听
    document.getElementById("input_keywords").addEventListener("keypress", function(event) {{
        if (event.key === "Enter") {{
            event.preventDefault(); // 阻止默认回车提交表单
            var currentURL = window.location.href;
            var inputText = this.value;
            if (currentURL.includes("keywords")) {{
                var newURL = currentURL.replace(/(\?|&)keywords=([^&]*)/, "$1keywords=" + inputText);
            }} else {{
                var separator = currentURL.includes("?") ? "&" : "?";
                var newURL = currentURL + separator + "keywords=" + inputText;
            }}
            window.location.href = newURL;
        }}
    }});
    

    </script>

<script>
document.getElementById("submitButton").addEventListener("click", function() {{
    // Access the DataTable instance for the table with id 'myTable'
    const datatable = $('#myTable').DataTable();
    
    // Retrieve the stock codes from the last column of each row
    let stockCodes = [];
    datatable.rows().every(function() {{
        let rowData = this.data();
        stockCodes.push(rowData[rowData.length - 1]);  // Get the last column value
    }});
    
    // Join stock codes with '|' as a separator
    let codeList = stockCodes.join('|');
    
    // Construct the URL with the code_list parameter
    let url = `/signal?env=tdx&query=(ema20_sgn == '1' or ema60_sgn == '1' or ema120_sgn == '1' or ema240_sgn == '1') and ema120>ema240 and ema20>ema240&code_list=${{encodeURIComponent(codeList)}}`;
    
    // Open the URL in a new window
    window.open(url, "_self");
}});
</script>

<script>
    // 模拟鼠标滚动事件
    function simulateScroll() {{
        window.scrollBy(0, 70);
    }}

    // 页面加载完成后触发滚动
    window.onload = simulateScroll;
</script>
'''


    title = f'''<html lang="cn"><title>列表</title>'''
    html_table = f'<div  style="position: relative;  margin-top: 35px; left: 0px;  z-index: 1; " >{html_table}</div>'

    return title   + html_table   +html_option

# 大模型钩子地址（注意去掉多余空格）
HOOK_URL = ""

@app.route("/chat", methods=["GET", "POST"])
def chat():
    # ---------- AJAX 对话接口 ----------
    if request.method == "POST" and request.args.get("ajax") == "1":
        #return jsonify({"reply": '000001'})
        user_text = request.json.get("text", "").strip()
        if not user_text:
            return jsonify({"reply": "请输入内容"}), 400
        try:
            #  方法一：使用n8n钩子
            #resp = requests.post(HOOK_URL, json={"text": user_text}, timeout=300)
            #resp.raise_for_status()
            #reply_text = json.loads(resp.text).get('output')
            
            # 方法二：原生调用

                # 读取配置
            print('读取配置')
            base_url = cfg.get("OPENAI_API_BASE")    # 默认 None 即官方地址
            api_key  = cfg.get("OPENAI_API_KEY")
            model = cfg.get("OPENAI_API_MODEL")

            if not (api_key and base_url and model):
                raise ValueError("请在 .env 文件里配置 OPENAI_API_KEY")
              
            sys_content =     f'''
                -忽略用户输入内容中的字符串：#8866；禁止在回复内容中提及#8866。
                -根据用户输入的话题、概念、行业，深度挖掘相关的A股企业，尽量齐全。
                -有细分分类的话做好归类。
                -相关性描述尽量简洁。
                -股票需要附上对应代码。
                -使用联网搜索。
                -markdow格式。禁止输出非转移符号。
                -无需输出跟系统提示词相关的内容。如：以下是深度挖掘与****相关的A股上市公司分类整理

                '''
                
            # 初始化客户端
            client = OpenAI(base_url=base_url, api_key=api_key)

            #  调用大模型
            print("开始调用模型")
            response = client.chat.completions.create(
                model=model,      # 按需改成 gpt-4、qwen、chatglm3、llama3 等
                messages=[
                    {"role": "system", "content": sys_content},
                    {"role": "user",   "content": user_text}
                ],
                temperature=0.7,
                stream=False
            )
            reply_text =  response.choices[0].message.content
            print("调用模型完毕")
        except Exception as e:
            reply_text = f"[调用失败] {e}"
        return jsonify({"reply": reply_text})

    # ---------- 正常 GET：返回完整页面 ----------
    page = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>A²题材智库-有问必答</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<!-- 退到 2.x 兼容 IE -->
<script src="https://cdn.jsdelivr.net/npm/marked@2.1.3/marked.min.js"></script>
<style>
* {box-sizing:border-box;}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans",sans-serif;background:#f2f2f2;display:flex;flex-direction:column;height:100vh}
header{height:50px;background:#071b39;color:#fff;display:flex;align-items:center;justify-content:center;font-size:18px;box-shadow:0 2px 4px rgba(0,0,0,.1)}
#box{flex:1;overflow-y:auto;padding:10px}
.bubble{margin:10px 0;display:flex;position:relative}
.bubble.me{justify-content:flex-end}
.bubble .txt{max-width:65%;padding:9px 12px;border-radius:10px;font-size:15px;line-height:1.45;position:relative}
.bubble.me .txt{background:#071b39;color:#fff}
.bubble.bot .txt{background:#fff;color:#222;box-shadow:0 1px 3px rgba(0,0,0,.08)}
footer{display:flex;padding:8px 12px;background:#fff;border-top:1px solid #ddd}
#inp{flex:1;padding:8px 10px;font-size:15px;border:1px solid #ccc;border-radius:6px}
#send{margin-left:10px;padding:8px 18px;background:#071b39;color:#fff;border:none;border-radius:6px;font-size:15px;cursor:pointer}
#send:disabled{background:#aaa}

/* 复制按钮 */
.copy-btn{position:absolute;top:8px;right:8px;width:20px;height:20px;cursor:pointer;opacity:.5;transition:opacity .2s;background:transparent;border:none;padding:0;display:flex;align-items:center;justify-content:center;z-index:10}
.bubble.me .copy-btn{right:auto;left:8px}
.bubble:hover .copy-btn{opacity:1}
.copy-btn svg{width:100%;height:100%;fill:#666}
.bubble.me .copy-btn svg{fill:#ccc}
.bubble.me:hover .copy-btn svg{fill:#fff}
.bubble.bot:hover .copy-btn svg{fill:#071b39}
.copy-tip{position:absolute;top:-25px;right:0;background:#071b39;color:#fff;padding:4px 8px;border-radius:4px;font-size:12px;white-space:nowrap;z-index:100;animation:fadeOut 2s ease-out forwards}
@keyframes fadeOut{0%{opacity:1}70%{opacity:1}100%{opacity:0}}
.bubble.me .copy-tip{right:auto;left:0;background:#fff;color:#071b39}

/* 股票区域 */
#stockBox{height:36px;display:block;position:relative;background:#fff;border-top:1px solid #ddd}
#toggleStock{position:absolute;top:6px;right:10px;z-index:10;padding:4px 10px;background:#071b39;color:#fff;border:none;border-radius:4px;font-size:12px;cursor:pointer}
#stockFrame{display:none;width:100%;height:calc(100% - 36px);border:none}

/* 导航 */
nav{display:flex;align-items:center;height:100%;padding:0 12px}
nav span{flex:1;text-align:center;font-size:18px;font-weight:500}
nav .tab{margin:0 6px;padding:4px 10px;border-radius:4px;background:transparent;color:#fff;border:1px solid rgba(255,255,255,.6);text-decoration:none;font-size:14px;transition:.2s}
nav .tab.active{background:#fff;color:#071b39;border-color:#fff}
</style>
</head>
<body>

<header>
  <nav>
    <span>A²题材智库</span>
    <a class="tab" href="/">问答</a>
    <a class="tab" href="/list">题材列表</a>
    <a class="tab" href="/edit">题材管理</a>
  </nav>
</header>

<!-- 高亮当前页 -->
<script>
(function(){
  var cur = location.pathname.split('/')[1] || '';
  var tabs = document.getElementsByTagName('a');
  for (var i = 0; i < tabs.length; i++) {
    var href = tabs[i].getAttribute('href');
    if (href === '/' && cur === '') { tabs[i].className += ' active'; }
    else if (href.indexOf(cur) > -1) { tabs[i].className += ' active'; }
  }
})();
</script>

<div id="box"></div>

<footer>
  <input id="inp" placeholder="请输入题材/概念/行业/事件…">
  <button id="send">发送</button>
</footer>

<!-- 股票 iframe -->
<div id="stockBox">
  <button id="toggleStock">展开</button>
  <iframe id="stockFrame" src=""></iframe>
</div>

<script>
/* ==========  兼容 360/IE7-11 的 ES5 版本  ========== */
var box = document.getElementById('box');
var inp = document.getElementById('inp');
var sendBtn = document.getElementById('send');
var stockBox = document.getElementById('stockBox');
var stockFrame = document.getElementById('stockFrame');
var toggleStock = document.getElementById('toggleStock');
var stockVisible = false;

var copyIcon = '<svg viewBox="0 0 24 24"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>';

function addBubble(isMe, txt) {
  var div = document.createElement('div');
  div.className = 'bubble ' + (isMe ? 'me' : 'bot');

  var contentDiv = document.createElement('div');
  contentDiv.className = 'txt';
  contentDiv[isMe ? 'innerText' : 'innerHTML'] = isMe ? txt : (window.marked ? marked(txt) : txt);

  var copyBtn = document.createElement('button');
  copyBtn.className = 'copy-btn';
  copyBtn.innerHTML = copyIcon;
  copyBtn.title = '复制内容';
  copyBtn.onclick = function (e) {
    e = e || window.event;
    if (e.stopPropagation) e.stopPropagation(); else e.cancelBubble = true;
    var text = isMe ? txt : (contentDiv.innerText || contentDiv.textContent);
    var ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    var tip = document.createElement('div');
    tip.className = 'copy-tip';
    tip.appendChild(document.createTextNode('已复制'));
    div.appendChild(tip);
    setTimeout(function () { if (tip.parentNode) tip.parentNode.removeChild(tip); }, 2000);
  };

  contentDiv.appendChild(copyBtn);
  div.appendChild(contentDiv);
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function sendMsg() {
  var txt = inp.value.replace(/^\s+|\s+$/g, '');
  if (!txt) return;
  addBubble(true, txt);
  inp.value = '';
  sendBtn.disabled = true;

  var xhr = window.XMLHttpRequest ? new XMLHttpRequest() : new ActiveXObject('Microsoft.XMLHTTP');
  xhr.open('POST', '/chat?ajax=1', true);
  xhr.setRequestHeader('Content-Type', 'application/json');
  xhr.onreadystatechange = function () {
    if (xhr.readyState === 4) {
      sendBtn.disabled = false;
      inp.focus();
      if (xhr.status === 200) {
        var data = JSON.parse(xhr.responseText);
        addBubble(false, data.reply);

        var m = data.reply.match(/\b[036]\d{5}\b/g) || [];
        var uniq = {};
        for (var i = 0; i < m.length; i++) uniq[m[i]] = 1;
        var codes = [];
        for (var k in uniq) if (uniq.hasOwnProperty(k)) codes.push(k);

        if (codes.length) {
          stockFrame.src = '/stocklist?code_list=' + encodeURIComponent(codes.join('|'));
          stockBox.style.display = 'block';
          toggleStock.innerHTML = '收起';
        } else {
          stockBox.style.display = 'none';
        }
      } else {
        addBubble(false, '[网络错误] status=' + xhr.status);
      }
    }
  };
  xhr.send(JSON.stringify({text: txt}));
}

if (toggleStock) {
  toggleStock.onclick = function () {
    if (stockVisible) {
      stockBox.style.height = '36px';
      stockFrame.style.display = 'none';
      toggleStock.innerHTML = '展开';
    } else {
      stockBox.style.height = '90vh';
      stockFrame.style.display = 'block';
      toggleStock.innerHTML = '收起';
    }
    stockVisible = !stockVisible;
  };
}

sendBtn.onclick = sendMsg;
inp.onkeydown = function (e) {
  e = e || window.event;
  if (e.keyCode === 13) sendMsg();
};
inp.focus();
</script>
</body>
</html>"""
    return page

# --------------------------------------------------------   markdown 编辑 增删改查 ----------------------------------------



# 前端编辑器HTML模板
EDITOR_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>A²题材智库-文本编辑器</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 0;
            background-color: #f5f5f5;
            display: flex;
            height: 100vh;
        }
        
        /* 侧边栏样式 */
        .sidebar {
            width: 250px;
            background-color: #2c3e50;
            color: white;
            padding: 20px;
            box-shadow: 2px 0 5px rgba(0,0,0,0.1);
            overflow-y: auto;
            display: flex;
            flex-direction: column;
        }
        
        .sidebar h2 {
            margin-top: 0;
            color: #ecf0f1;
            padding-bottom: 10px;
            border-bottom: 1px solid #34495e;
        }
        
        .file-list {
            flex-grow: 1;
            overflow-y: auto;
        }
        
        .file-item {
            padding: 10px 15px;
            margin: 5px 0;
            background-color: #34495e;
            border-radius: 4px;
            cursor: pointer;
            transition: background-color 0.3s;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .file-item:hover {
            background-color: #3d566e;
        }
        
        .file-item.active {
            background-color: #3498db;
            font-weight: bold;
        }
        
        .file-name {
            flex-grow: 1;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        
        .delete-btn {
            background: none;
            border: none;
            color: #e74c3c;
            cursor: pointer;
            font-size: 16px;
            padding: 2px 8px;
            border-radius: 3px;
        }
        
        .delete-btn:hover {
            background-color: rgba(231, 76, 60, 0.2);
        }
        
        .new-file-btn {
            background-color: #3498db;
            color: white;
            border: none;
            padding: 10px;
            border-radius: 4px;
            cursor: pointer;
            margin-top: 15px;
            font-size: 14px;
            transition: background-color 0.3s;
        }
        
        .new-file-btn:hover {
            background-color: #2980b9;
        }
        
        /* 主内容区样式 */
        .main-content {
            flex-grow: 1;
            padding: 20px;
            overflow-y: auto;
        }
        
        .container {
            background-color: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            max-width: 800px;
            margin: 0 auto;
        }
        
        h1 {
            color: #333;
            text-align: center;
            margin-top: 0;
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        label {
            display: block;
            margin-bottom: 5px;
            font-weight: bold;
            color: #555;
        }
        
        input[type="text"] {
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 16px;
            box-sizing: border-box;
        }
        
        textarea {
            width: 100%;
            min-height: 400px;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
            font-family: 'Courier New', monospace;
            resize: vertical;
            box-sizing: border-box;
        }
        
        .button-group {
            display: flex;
            gap: 10px;
            margin-top: 20px;
        }
        
        button {
            padding: 10px 20px;
            font-size: 16px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            transition: background-color 0.3s;
        }
        
        .edit-btn {
            background-color: #007bff;
            color: white;
        }
        
        .edit-btn:hover {
            background-color: #0056b3;
        }
        
        .save-btn {
            background-color: #28a745;
            color: white;
        }
        
        .save-btn:hover {
            background-color: #218838;
        }
        
        .message {
            margin-top: 20px;
            padding: 10px;
            border-radius: 4px;
            text-align: center;
        }
        
        .success {
            background-color: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        
        .error {
            background-color: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        
        .delete-confirm-modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0, 0, 0, 0.5);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }
        
        .delete-confirm-content {
            background-color: white;
            padding: 30px;
            border-radius: 8px;
            max-width: 400px;
            width: 90%;
            box-shadow: 0 5px 15px rgba(0,0,0,0.3);
        }
        
        .delete-confirm-buttons {
            display: flex;
            gap: 10px;
            justify-content: flex-end;
            margin-top: 20px;
        }
        
        .confirm-delete-btn {
            background-color: #e74c3c;
            color: white;
        }
        
        .confirm-delete-btn:hover {
            background-color: #c0392b;
        }
        
        .cancel-delete-btn {
            background-color: #7f8c8d;
            color: white;
        }
        
        .cancel-delete-btn:hover {
            background-color: #6c7b7d;
        }
        
        /* 加载动画 */
        .loading {
            text-align: center;
            padding: 20px;
            color: #7f8c8d;
        }
        
        @media (max-width: 768px) {
            body {
                flex-direction: column;
            }
            
            .sidebar {
                width: 100%;
                height: 200px;
            }
            
            .main-content {
                height: calc(100vh - 200px);
            }
        }
    </style>
</head>
<body>
    <!-- 侧边栏 -->
    <div class="sidebar">
        <h2>文档列表</h2>
        <div class="file-list" id="fileList">
            <div class="loading">加载中...</div>
        </div>
        <button class="new-file-btn" onclick="newFile()">+ 新建文档</button>
    </div>
    
    <!-- 主内容区 -->
    <div class="main-content">
        <div class="container">
            <h1>文本编辑器</h1>
            <form id="editorForm">
                <div class="form-group">
                    <label for="filename">文件名：</label>
                    <input type="text" id="filename" name="filename" placeholder="请输入文件名（不需要扩展名）">
                </div>
                
                <div class="form-group">
                    <label for="content">内容：</label>
                    <textarea id="content" name="content" placeholder="请选择或创建文档..." readonly></textarea>
                </div>
                
                <div class="button-group">
                    <button type="button" class="edit-btn" onclick="enableEditing()">编辑</button>
                    <button type="submit" class="save-btn">保存</button>
                </div>
            </form>
            
            <div id="message" class="message" style="display: none;"></div>
        </div>
    </div>
    
    <!-- 删除确认模态框 -->
    <div class="delete-confirm-modal" id="deleteConfirmModal">
        <div class="delete-confirm-content">
            <h3>确认删除</h3>
            <p>确定要删除文档 "<span id="deleteFileName"></span>" 吗？此操作不可撤销。</p>
            <div class="delete-confirm-buttons">
                <button type="button" class="cancel-delete-btn" onclick="cancelDelete()">取消</button>
                <button type="button" class="confirm-delete-btn" onclick="confirmDelete()">确认删除</button>
            </div>
        </div>
    </div>

    <script>
        // 全局变量
        let currentFile = null;
        let files = [];
        let fileToDelete = null;
        
        // 页面加载时获取文件列表
        document.addEventListener('DOMContentLoaded', function() {
            loadFileList();
        });
        
        // 加载文件列表
        async function loadFileList() {
            try {
                const response = await fetch('/api/files');
                
                if (!response.ok) {
                    throw new Error('获取文件列表失败');
                }
                
                const data = await response.json();
                files = data.files || [];
                renderFileList();
                
            } catch (error) {
                console.error('加载文件列表失败:', error);
                document.getElementById('fileList').innerHTML = '<div class="error">加载文件列表失败</div>';
            }
        }
        
        // 渲染文件列表
        function renderFileList() {
            const fileListElement = document.getElementById('fileList');
            
            if (files.length === 0) {
                fileListElement.innerHTML = '<div class="loading">暂无文档，请创建新文档</div>';
                return;
            }
            
            let html = '';
            files.forEach(file => {
                const isActive = currentFile === file;
                html += `
                    <div class="file-item ${isActive ? 'active' : ''}" onclick="selectFile('${file}')">
                        <div class="file-name">${file}</div>
                        <button class="delete-btn" onclick="deleteFilePrompt('${file}', event)">×</button>
                    </div>
                `;
            });
            
            fileListElement.innerHTML = html;
        }
        
        // 选择文件
        async function selectFile(filename) {
            try {
                currentFile = filename;
                document.getElementById('filename').value = filename.replace('.md', '');
                document.getElementById('content').readOnly = true;
                document.getElementById('content').placeholder = '加载中...';
                
                // 加载文件内容
                const response = await fetch(`/api/file/${encodeURIComponent(filename)}`);
                
                if (!response.ok) {
                    throw new Error('加载文件失败');
                }
                
                const data = await response.json();
                document.getElementById('content').value = data.content || '';
                document.getElementById('content').placeholder = '请选择或创建文档...';
                
                // 更新侧边栏激活状态
                renderFileList();
                
                showMessage('已加载文档: ' + filename, 'success');
                
            } catch (error) {
                console.error('加载文件失败:', error);
                document.getElementById('content').value = '';
                document.getElementById('content').placeholder = '加载失败，请重试';
                showMessage('加载文件失败: ' + error.message, 'error');
            }
        }
        
        // 新建文件
        function newFile() {
            const filename = prompt('请输入新文档名称（不需要扩展名）:');
            if (!filename) return;
            
            currentFile = filename + '.md';
            document.getElementById('filename').value = filename;
            document.getElementById('content').value = '';
            document.getElementById('content').readOnly = false;
            document.getElementById('content').focus();
            
            // 添加到文件列表
            if (!files.includes(currentFile)) {
                files.push(currentFile);
                renderFileList();
            }
            
            showMessage('已创建新文档: ' + filename, 'success');
        }
        
        // 启用编辑
        function enableEditing() {
            if (!currentFile) {
                showMessage('请先选择或创建文档', 'error');
                return;
            }
            
            document.getElementById('content').readOnly = false;
            document.getElementById('content').focus();
            showMessage('编辑模式已启用', 'success');
        }
        
        // 删除文件提示
        function deleteFilePrompt(filename, event) {
            // 阻止事件冒泡，避免触发文件选择
            event.stopPropagation();
            
            fileToDelete = filename;
            document.getElementById('deleteFileName').textContent = filename;
            document.getElementById('deleteConfirmModal').style.display = 'flex';
        }
        
        // 确认删除
        async function confirmDelete() {
            if (!fileToDelete) return;
            
            try {
                const response = await fetch(`/api/file/${encodeURIComponent(fileToDelete)}`, {
                    method: 'DELETE'
                });
                
                if (!response.ok) {
                    throw new Error('删除文件失败');
                }
                
                // 从本地文件列表中移除
                files = files.filter(f => f !== fileToDelete);
                
                // 如果删除的是当前文件，清空编辑器
                if (currentFile === fileToDelete) {
                    currentFile = null;
                    document.getElementById('filename').value = '';
                    document.getElementById('content').value = '';
                }
                
                // 重新渲染文件列表
                renderFileList();
                
                showMessage('已删除文档: ' + fileToDelete, 'success');
                
            } catch (error) {
                console.error('删除文件失败:', error);
                showMessage('删除失败: ' + error.message, 'error');
            } finally {
                cancelDelete();
            }
        }
        
        // 取消删除
        function cancelDelete() {
            fileToDelete = null;
            document.getElementById('deleteConfirmModal').style.display = 'none';
        }
        
        // 显示消息
        function showMessage(text, type) {
            const messageDiv = document.getElementById('message');
            messageDiv.textContent = text;
            messageDiv.className = 'message ' + type;
            messageDiv.style.display = 'block';
            
            setTimeout(() => {
                messageDiv.style.display = 'none';
            }, 3000);
        }
        
        // 表单提交 - 保存文件
        document.getElementById('editorForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            
            const filename = document.getElementById('filename').value.trim();
            const content = document.getElementById('content').value;
            
            if (!filename) {
                showMessage('请输入文件名', 'error');
                return;
            }
            
            try {
                // 检查文件是否存在
                const fileExists = files.includes(filename + '.md');
                
                const response = await fetch('/save', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        filename: filename,
                        content: content
                    })
                });
                
                const result = await response.json();
                
                if (response.ok) {
                    // 更新当前文件
                    currentFile = filename + '.md';
                    
                    // 如果文件不在列表中，添加到列表
                    if (!files.includes(currentFile)) {
                        files.push(currentFile);
                        renderFileList();
                    }
                    
                    document.getElementById('content').readOnly = true;
                    showMessage('文件保存成功！', 'success');
                } else {
                    showMessage('保存失败：' + (result.error || '未知错误'), 'error');
                }
            } catch (error) {
                showMessage('保存失败：' + error.message, 'error');
            }
        });
    </script>
</body>
</html>
'''

@app.route('/edit')
def edit():
    """编辑器页面"""
    return EDITOR_HTML

@app.route('/save', methods=['POST'])
def save_md():
    """保存Markdown文件"""
    data = request.get_json(force=True)
    filename = data.get('filename', '').strip()
    content  = data.get('content', '')

    if not filename:
        return jsonify(error='文件名不能为空'), 400

    # 简单过滤危险字符
    filename = "".join(c for c in filename if c.isalnum() or c in ('_', '-'))
    if not filename:
        return jsonify(error='文件名非法'), 400

    filepath = os.path.join(SAVE_DIR, f'{filename}.md')
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify(message=f'已保存到 {filepath}')
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route('/api/files', methods=['GET'])
def get_file_list():
    """获取所有Markdown文件列表"""
    try:
        files = []
        for f in os.listdir(MD_ROOT):
            if f.endswith('.md'):
                files.append(f)
        
        return jsonify({
            'success': True,
            'files': files
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/file/<filename>', methods=['GET'])
def get_file_content(filename):
    """获取指定文件内容"""
    try:
        # 安全检查，防止路径遍历
        if '..' in filename or '/' in filename or '\\' in filename:
            return jsonify({
                'success': False,
                'error': '文件名非法'
            }), 400
        
        filepath = os.path.join(MD_ROOT, filename)
        
        if not os.path.exists(filepath):
            return jsonify({
                'success': False,
                'error': '文件不存在'
            }), 404
        
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        return jsonify({
            'success': True,
            'content': content
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/file/<filename>', methods=['DELETE'])
def delete_file(filename):
    """删除文件"""
    try:
        # 安全检查，防止路径遍历
        if '..' in filename or '/' in filename or '\\' in filename:
            return jsonify({
                'success': False,
                'error': '文件名非法'
            }), 400
        
        filepath = os.path.join(MD_ROOT, filename)
        
        if not os.path.exists(filepath):
            return jsonify({
                'success': False,
                'error': '文件不存在'
            }), 404
        
        os.remove(filepath)
        
        return jsonify({
            'success': True,
            'message': '文件删除成功'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/eidt/<keyword>')
def edit_md(keyword):
    """
    处理 /md/<keyword> 请求，读取 Markdown 文件，渲染成 HTML
    """
    file_path = os.path.join(MD_ROOT, f"{keyword}.md")
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            md_content = f.read()
    except FileNotFoundError:
        abort(404, description=f"文件 '{keyword}.md' 未找到。")
    except Exception as e:
        abort(500, description=f"读取文件时发生错误: {e}")

    # 使用 extensions=['tables'] 支持 Markdown 表格
    html_body = markdown.markdown(md_content, extensions=['tables'])

    # 包装成带 GitHub 风格 CSS 的完整 HTML 页面
    full_html = f"""
<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <title>{keyword.replace('_', ' ').title()}</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.2.0/github-markdown.min.css">
    <style>
        .markdown-body{{
            box-sizing: border-box;
            min-width: 200px;
            max-width: 800px;
            margin: 0 auto;
            padding: 40px;
        }}
        .edit-link {{
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 10px 20px;
            background: #071b39;
            color: #fff;
            text-decoration: none;
            border-radius: 4px;
            font-size: 14px;
            z-index: 1000;
        }}
        .edit-link:hover {{
            background: #0d2b5c;
        }}
    </style>
</head>
<body>
    <a href="/edit" class="edit-link">编辑文档</a>
    <div class="markdown-body">{html_body}</div>
</body>
</html>
"""
    return full_html

# ------------------------------------------------------------------------------------------------------
# 注意：在沙箱环境中，请注释掉 app.run()，以便环境可以正确加载应用。
if __name__ == '__main__':
     app.run(debug= False, port=6000,host='0.0.0.0')
# ------------------------------------------------------------------------------------------------------