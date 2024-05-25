from freegenius import config
from freegenius.utils.terminal_mode_dialogs import TerminalModeDialogs
import sys, os, geocoder, platform, socket, geocoder, datetime, requests, netifaces, getpass, pendulum, pkg_resources, webbrowser, unicodedata
import traceback, uuid, re, textwrap, signal, wcwidth, shutil, threading, time, tiktoken, subprocess, json, base64, html2text, pydoc, codecs, psutil
from packaging import version
from chromadb.utils import embedding_functions
from pygments.styles import get_style_by_name
from prompt_toolkit.styles.pygments import style_from_pygments_cls
from prompt_toolkit import print_formatted_text, HTML
from prompt_toolkit import prompt
from typing import Optional, Any
from vertexai.generative_models import Content, Part
from pathlib import Path
from PIL import Image
from openai import OpenAI
from huggingface_hub import hf_hub_download
from bs4 import BeautifulSoup
from urllib.parse import quote
from guidance import select, gen
from typing import Union
from transformers import pipeline
from groq import Groq
from ollama import Client


# non-Android only
if not config.isTermux:
    from autogen.retrieve_utils import TEXT_FORMATS

# a dummy import line to resolve ALSA error display on Linux
import sounddevice

# transformers

def classify(user_input, candidate_labels):
    classifier = pipeline(task="zero-shot-classification", model=config.zero_shot_classification_model)
    response = classifier(
        user_input,
        candidate_labels=candidate_labels,
    )
    labels = response["labels"]
    return labels[0]

def isToolRequired(user_input) -> bool:
    tool = True
    print2("```screening")
    # check the kind of input
    kind = classify(user_input, config.labels_kind)
    print3(f"Kind: {kind}")
    if kind in config.labels_kind_chat_only_options:
        tool = False
    elif kind in config.labels_kind_information_options:
        # check the nature of the requested information
        information = classify(user_input, config.labels_information)
        print3(f"Information: {information}")
        if information in config.labels_information_chat_only_options:
            tool = False
    else:
        # check the nature of the requested response
        action = classify(user_input, config.labels_action)
        print3(f"Action: {action}")
        if action in config.labels_action_chat_only_options:
            tool = False
    print3(f"""Comment: Tool may {"" if tool else "not "}be required.""")
    print2("```")
    return tool

# guidance

def screening(lm, user_input) -> bool:
    tool = False

    print2("```screening")
    thought = "Question: Is the given request formulated like a greeting, a question, a command, a statement, an issue, a description?"
    print3(thought)
    lm += f"""<|im_start|>user
Please answer my questions with regards to the following request:
<request>{user_input}</request>
<|im_end|>
<|im_start|>assistant
Certainly! Please provide me with the questions.
<|im_end|>
<|im_start|>user
{thought}
Answer: The given request is formulated like {select(["a question", "a command", "a statement", "an issue", "a description"], name="question")}.
<|im_end|>
"""
    question = lm.get("question")
    print3(f"""Answer: The given request is formulated like {question}.""")
    if question in ("a greeting", "a question", "an issue", "a description"):
        thought = "Question: What is the request about?"
        print3(thought)
        lm += f"""<|im_start|>assistant
{thought}
<|im_end|>
<|im_start|>user
Answer: The request is about {select(["greeting", "common knowledge", "math", "published content", "trained knowledge", "historical records", "programming knowledge", "religious knowledge", "insights obtainable from literature", "textbook content", "evolving data", "recent updates", "latest information", "current time", "current weather", "up-to-date news", "information specific to your device", "information unknown to me"], name="information")}.
<|im_end|>
"""
        information = lm.get("information")
        print3(f"""Answer: The request is about {information}.""")
        if information in ("evolving data", "recent updates", "latest information", "current time", "current weather", "up-to-date news", "information specific to your device", "information unknown to me"):
            tool = True
    else:
        thought = "Question: Does the given request ask for generating a text-response or carrying out a task on your device?"
        print3(thought)
        lm += f"""<|im_start|>assistant
{thought}
<|im_end|>
<|im_start|>user
Answer: The given request asks for {select(["greeting", "calculation", "translation", "writing a text-response", "carrying out a task on your device"], name="action")}.
<|im_end|>
"""
        action = lm.get("action")
        print3(f"""Answer: The given request asks for {action}.""")
        if action in ("carrying out a task on your device",):
            tool = True

    print3(f"""Comment: Tool may {"" if tool else "not "}be required.""")
    print2("```")

    return tool

def outputStructuredData(lm, schema: dict, json_output: bool=False, messages: list = [], use_system_message: bool=True, request: str="", temperature: Optional[float]=None, max_tokens: Optional[int]=None, **kwargs) -> Union[dict, str]:
    properties = toParameterSchema(schema)["properties"]
    request = f", particularly related to the following request:\n{request}" if request else "."
    lm += toChatml(messages, use_system_message=use_system_message).rstrip()
    lm += f"""<|im_start|>assistant.
I am answering your questions based on the content in our conversation given above{request}
<|im_end|>
"""
    for key, value in properties.items():
        description = value["description"].replace("\n", " ")
        if "enum" in value:
            options = value["enum"]
            options_str = "', '".join(value["enum"])
            description += f" Its value must be one of these options: '{options_str}'"
        lm += f'''<|im_start|>user
Question: {description}
<|im_end|>
<|im_start|>assistant
Answer: {select(options, name=key) if "enum" in value else gen(name=key, stop="<")}
<|im_end|>
'''

    response = {}
    for i in properties:
        response[i] = codecs.decode(lm.get(i, "").rstrip(), "unicode_escape")
    return json.dumps(response) if json_output else response

def select_tool(lm, user_input):
    tool_names = list(config.toolFunctionSchemas.keys())
    tools = {i:config.toolFunctionSchemas[i]["description"] for i in config.toolFunctionSchemas}

    lm += f"""<|im_start|>user
Select an action to resolve the request as best you can. You have access only to the following tools:

{tools}

Use the following format:

Request: the input request you must resolve
Thought: you should always think about what to do
Action: the action to take, has to be one of {tool_names}<|im_end|>
<|im_start|>assistant
Request: {user_input}
Thought: {gen(stop=".")}.
Action: {select(tool_names, name="tool")}"""
    
    return lm.get("tool")

# llm

def getGroqApi_key():
    '''
    support multiple grop api keys to work around rate limit
    User can manually edit config to change the value of config.groqApi_key to a list of multiple api keys instead of a string of a single api key
    '''
    if config.groqApi_key:
        if isinstance(config.groqApi_key, str):
            return config.groqApi_key
        elif isinstance(config.groqApi_key, list):
            if len(config.groqApi_key) > 1:
                # rotate multiple api keys
                config.groqApi_key = config.groqApi_key[1:] + [config.groqApi_key[0]]
            return config.groqApi_key[0]
        else:
            return ""
    else:
        return ""

def getGroqClient():
    return Groq(api_key=getGroqApi_key())

def downloadStableDiffusionFiles():
    # llm directory
    llm_directory = os.path.join(config.localStorage, "LLMs", "stable_diffusion")
    Path(llm_directory).mkdir(parents=True, exist_ok=True)
    filename = "v1-5-pruned-emaonly.safetensors"
    stableDiffusion_model_path = os.path.join(llm_directory, filename)
    if not config.stableDiffusion_model_path or not os.path.isfile(config.stableDiffusion_model_path):
        config.stableDiffusion_model_path = stableDiffusion_model_path

    if not os.path.isfile(config.stableDiffusion_model_path):
        print2("Downloading stable-diffusion model ...")
        hf_hub_download(
            repo_id="runwayml/stable-diffusion-v1-5",
            filename=filename,
            local_dir=llm_directory,
            #local_dir_use_symlinks=False,
        )
        stableDiffusion_model_path = os.path.join(llm_directory, filename)
        if os.path.isfile(stableDiffusion_model_path):
            config.stableDiffusion_model_path = stableDiffusion_model_path
            config.saveConfig()

    llm_directory = os.path.join(llm_directory, "lora")
    filename = "pytorch_lora_weights.safetensors"
    lora_file = os.path.join(llm_directory, filename)
    if not os.path.isfile(lora_file):
        print2("Downloading stable-diffusion LCM-LoRA ...")
        hf_hub_download(
            repo_id="latent-consistency/lcm-lora-sdv1-5",
            filename=filename,
            local_dir=llm_directory,
            #local_dir_use_symlinks=False,
        )
        stableDiffusion_model_path = os.path.join(llm_directory, filename)

def startAutogenstudioServer():
    try:
        if not hasattr(config, "autogenstudioServer") or config.autogenstudioServer is None:
            config.autogenstudioServer = None
            print2("Running Autogen Studio server ...")
            cmd = f"""{sys.executable} -m autogenstudio.cli ui --host 127.0.0.1 --port {config.autogenstudio_server_port}"""
            config.autogenstudioServer = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, preexec_fn=os.setsid)
            while not isServerAlive("127.0.0.1", config.autogenstudio_server_port):
                # wait til the server is up
                ...
    except:
        print2(f'''Failed to run Autogen Studio server at "localhost:{config.autogenstudio_server_port}"!''')
        config.autogenstudioServer = None
    webbrowser.open(f"http://127.0.0.1:{config.autogenstudio_server_port}")

def stopAutogenstudioServer():
    if hasattr(config, "autogenstudioServer") and config.autogenstudioServer is not None:
        if isServerAlive("127.0.0.1", config.autogenstudio_server_port):
            print2("Stopping Autogen Studio server ...")
            os.killpg(os.getpgid(config.autogenstudioServer.pid), signal.SIGTERM)
        config.autogenstudioServer = None

def getOllamaServerClient(server="main"):
    return Client(host=f"http://{config.ollamaChatServer_ip if server=='chat' else config.ollamaToolServer_ip}:{config.ollamaChatServer_port if server=='chat' else config.ollamaToolServer_port}")

def getLlamacppServerClient(server="main"):
    return OpenAI(
        base_url=f"http://{config.customChatServer_ip if server=='chat' else config.customToolServer_ip}:{config.customChatServer_port if server=='chat' else config.customToolServer_port}/v1",
        api_key = "freegenius"
    )

def startLlamacppServer():
    try:
        if not hasattr(config, "llamacppServer") or config.llamacppServer is None:
            config.llamacppServer = None
            print2("Running llama.cpp tool server ...")
            cpuThreads = getCpuThreads()
            cmd = f"""{sys.executable} -m llama_cpp.server --port {config.llamacppMainModel_server_port} --model "{config.llamacppMainModel_model_path}" --verbose {config.llamacppMainModel_verbose} --chat_format chatml --n_ctx {config.llamacppMainModel_n_ctx} --n_gpu_layers {config.llamacppMainModel_n_gpu_layers} --n_batch {config.llamacppMainModel_n_batch} --n_threads {cpuThreads} --n_threads_batch {cpuThreads} {config.llamacppMainModel_additional_server_options}"""
            config.llamacppServer = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, preexec_fn=os.setsid)
            while not isServerAlive("127.0.0.1", config.llamacppMainModel_server_port):
                # wait til the server is up
                ...
    except:
        print2(f'''Failed to run llama.cpp server at "localhost:{config.llamacppMainModel_server_port}"!''')
        config.llamacppServer = None
    webbrowser.open(f"http://127.0.0.1:{config.llamacppMainModel_server_port}/docs")

def stopLlamacppServer():
    if hasattr(config, "llamacppServer") and config.llamacppServer is not None:
        if isServerAlive("127.0.0.1", config.llamacppMainModel_server_port):
            print2("Stopping llama.cpp tool server ...")
            os.killpg(os.getpgid(config.llamacppServer.pid), signal.SIGTERM)
        config.llamacppServer = None

def startLlamacppChatServer():
    try:
        if not hasattr(config, "llamacppChatServer") or config.llamacppChatServer is None:
            config.llamacppChatServer = None
            print2("Running llama.cpp chat server ...")
            cpuThreads = getCpuThreads()
            cmd = f"""{sys.executable} -m llama_cpp.server --port {config.llamacppChatModel_server_port} --model "{config.llamacppChatModel_model_path}" --verbose {config.llamacppChatModel_verbose} --chat_format chatml --n_ctx {config.llamacppChatModel_n_ctx} --n_gpu_layers {config.llamacppChatModel_n_gpu_layers} --n_batch {config.llamacppChatModel_n_batch} --n_threads {cpuThreads} --n_threads_batch {cpuThreads} {config.llamacppChatModel_additional_server_options}"""
            config.llamacppChatServer = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, preexec_fn=os.setsid)
            while not isServerAlive("127.0.0.1", config.llamacppChatModel_server_port):
                # wait til the server is up
                ...
    except:
        print2(f'''Failed to run llama.cpp server at "localhost:{config.llamacppChatModel_server_port}"!''')
        config.llamacppChatServer = None
    webbrowser.open(f"http://127.0.0.1:{config.llamacppChatModel_server_port}/docs")

def stopLlamacppChatServer():
    if hasattr(config, "llamacppChatServer") and config.llamacppChatServer is not None:
        if isServerAlive("127.0.0.1", config.llamacppChatModel_server_port):
            print2("Stopping llama.cpp chat server ...")
            os.killpg(os.getpgid(config.llamacppChatServer.pid), signal.SIGTERM)
        config.llamacppChatServer = None

def startLlamacppVisionServer():
    try:
        if not hasattr(config, "llamacppVisionServer") or config.llamacppVisionServer is None:
            if os.path.isfile(config.llamacppVisionModel_model_path) and os.path.isfile(config.llamacppVisionModel_clip_model_path):
                config.llamacppVisionServer = None
                print2("Running llama.cpp vision server ...")
                cpuThreads = getCpuThreads()
                cmd = f"""{sys.executable} -m llama_cpp.server --port {config.llamacppVisionModel_server_port} --model "{config.llamacppVisionModel_model_path}" --clip_model_path {config.llamacppVisionModel_clip_model_path} --verbose {config.llamacppVisionModel_verbose} --chat_format llava-1-5 --n_ctx {config.llamacppVisionModel_n_ctx} --n_gpu_layers {config.llamacppVisionModel_n_gpu_layers} --n_batch {config.llamacppVisionModel_n_batch} --n_threads {cpuThreads} --n_threads_batch {cpuThreads} {config.llamacppVisionModel_additional_server_options}"""
                config.llamacppVisionServer = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, preexec_fn=os.setsid)
                while not isServerAlive("127.0.0.1", config.llamacppVisionModel_server_port):
                    # wait til the server is up
                    ...
            else:
                print1("Error! Clip model or vision model is missing!")
    except:
        print2(f'''Failed to run llama.cpp server at "localhost:{config.llamacppVisionModel_server_port}"!''')
        config.llamacppVisionServer = None
    webbrowser.open(f"http://127.0.0.1:{config.llamacppVisionModel_server_port}/docs")

def stopLlamacppVisionServer():
    if hasattr(config, "llamacppVisionServer") and config.llamacppVisionServer is not None:
        if isServerAlive("127.0.0.1", config.llamacppVisionModel_server_port):
            print2("Stopping llama.cpp vision server ...")
            os.killpg(os.getpgid(config.llamacppVisionServer.pid), signal.SIGTERM)
        config.llamacppVisionServer = None

def getOllamaModelDir():
    # read https://github.com/ollama/ollama/blob/main/docs/faq.md#where-are-models-stored
    OLLAMA_MODELS = os.getenv("OLLAMA_MODELS")
    if not OLLAMA_MODELS or (OLLAMA_MODELS and not os.path.isdir(OLLAMA_MODELS)):
        os.environ['OLLAMA_MODELS'] = ""

    if os.environ['OLLAMA_MODELS']:
        return os.environ['OLLAMA_MODELS']
    elif config.thisPlatform == "Windows":
        modelDir = os.path.expanduser("~\.ollama\models")
    elif config.thisPlatform == "macOS":
        modelDir = os.path.expanduser("~/.ollama/models")
    elif config.thisPlatform == "Linux":
        modelDir = "/usr/share/ollama/.ollama/models"
    
    if os.path.isdir(modelDir):
        return modelDir
    return ""

def getDownloadedOllamaModels() -> dict:
    models = {}
    if modelDir := getOllamaModelDir():
        library = os.path.join(modelDir, "manifests", "registry.ollama.ai", "library")
        if os.path.isdir(library):
            for d in os.listdir(library):
                model_dir = os.path.join(library, d)
                if os.path.isdir(model_dir):
                    for f in os.listdir(model_dir):
                        manifest = os.path.join(model_dir, f)
                        if os.path.isfile(manifest):
                            try:
                                with open(manifest, "r", encoding="utf-8") as fileObj:
                                    content = fileObj.read()
                                model_file = re.search('''vnd.ollama.image.model","digest":"(.*?)"''', content)
                                if model_file:
                                    model_file = os.path.join(modelDir, "blobs", model_file.group(1))
                                    if not os.path.isfile(model_file):
                                        model_file = model_file.replace(":", "-")
                                    if os.path.isfile(model_file):
                                        model_tag = f"{d}:{f}"
                                        models[model_tag] = model_file
                                        if f == "latest":
                                            models[d] = model_file
                            except:
                                pass
    return models

def exportOllamaModels(selection: list=[]) -> None:
    llm_directory = os.path.join(config.localStorage, "LLMs", "gguf")
    Path(llm_directory).mkdir(parents=True, exist_ok=True)
    models = getDownloadedOllamaModels()
    for model, originalpath in models.items():
        filename = model.replace(":", "_")
        exportpath = os.path.join(llm_directory, f"{filename}.gguf")
        if not os.path.isfile(exportpath) and not model.endswith(":latest") and ((not selection) or (model in selection)):
            print3(f"Model: {model}")
            shutil.copy2(originalpath, exportpath)
            print3(f"Exported: {exportpath}")

def getDownloadedGgufModels() -> dict:
    llm_directory = os.path.join(config.localStorage, "LLMs", "gguf")
    models = {}
    for f in getFilenamesWithoutExtension(llm_directory, "gguf"):
        models[f] = os.path.join(llm_directory, f"{f}.gguf")
    return models

# text

def is_CJK(text):
    for char in text:
        if 'CJK' in unicodedata.name(char):
            return True
    return False

# Function to convert HTML to Markdown
def convert_html_to_markdown(html_string):
    # Create an instance of the HTML2Text converter
    converter = html2text.HTML2Text()
    # Convert the HTML string to Markdown
    markdown_string = converter.handle(html_string)
    # Return the Markdown string
    return markdown_string

# system command

def getCpuThreads():
    if config.cpu_threads and isinstance(config.cpu_threads, int):
        return config.cpu_threads
    physical_cpu_core = psutil.cpu_count(logical=False)
    return physical_cpu_core if physical_cpu_core and physical_cpu_core > 1 else 1

def getCliOutput(cli):
    try:
        process = subprocess.Popen(cli, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, *_ = process.communicate()
        return stdout.decode("utf-8")
    except:
        return ""

def textTool(tool="", content=""):
    command = re.sub(" .*?$", "", tool.strip())
    if command and isCommandInstalled(command):
        pydoc.pipepager(content, cmd=tool)
        if isCommandInstalled("pkill"):
            os.system(f"pkill {command}")
    return ""

def getHideOutputSuffix():
    return f" > {'nul' if config.thisPlatform == 'Windows' else '/dev/null'} 2>&1"

def runFreeGeniusCommand(command):
    def createShortcutFile(filePath, content):
        with open(filePath, "w", encoding="utf-8") as fileObj:
            fileObj.write(content)

    iconFile = os.path.join(config.freeGeniusAIFolder, "icons", "ai.png")

    shortcut_dir = os.path.join(config.freeGeniusAIFolder, "shortcuts")
    Path(shortcut_dir).mkdir(parents=True, exist_ok=True)

    # The following line does not work on Windows
    commandPath = os.path.join(os.path.dirname(sys.executable), command)

    if config.thisPlatform == "Windows":
        opencommand = "start"
        filePath = os.path.join(shortcut_dir, f"{command}.bat")
        if not os.path.isfile(filePath):
            filenames = {
                "freegenius": "main.py",
                "etextedit": "eTextEdit.py",
            }
            systemTrayFile = os.path.join(config.freeGeniusAIFolder, filenames.get(command, f"{command}.py"))
            content = f'''powershell.exe -NoExit -Command "{sys.executable} '{systemTrayFile}'"'''
            createShortcutFile(filePath, content)
    elif config.thisPlatform == "macOS":
        opencommand = "open"
        filePath = os.path.join(shortcut_dir, f"{command}.command")
        if not os.path.isfile(filePath):
            content = f"""#!/bin/bash
cd {config.freeGeniusAIFolder}
{commandPath}"""
            createShortcutFile(filePath, content)
            os.chmod(filePath, 0o755)
    elif config.thisPlatform == "Linux":
        opencommand = ""
        for i in ("gio launch", "dex", "exo-open", "xdg-open"):
            # Remarks:
            # 'exo-open' comes with 'exo-utils'
            # 'gio' comes with 'glib2'
            if shutil.which(i.split(" ", 1)[0]):
                opencommand = i
                break
        filePath = os.path.join(shortcut_dir, f"{command}.desktop")
        if not os.path.isfile(filePath):
            content = f"""[Desktop Entry]
Version=1.0
Type=Application
Terminal=true
Path={config.freeGeniusAIFolder}
Exec={commandPath}
Icon={iconFile}
Name={command}"""
            createShortcutFile(filePath, content)
    if opencommand:
        os.system(f"{opencommand} {filePath}")

# tool selection

def selectTool(search_result, closest_distance) -> Optional[int]:
    if closest_distance <= config.tool_auto_selection_threshold:
        # auto
        return 0
    else:
        # manual
        tool_options = []
        tool_descriptions = []
        for index, item in enumerate(search_result["metadatas"][0]):
            tool_options.append(str(index))
            tool_descriptions.append(item["name"].replace("_", " "))
        tool_options.append(str(len(search_result["metadatas"][0])))
        tool_descriptions.append("more ...")
        stopSpinning()
        tool = TerminalModeDialogs(None).getValidOptions(
            title="Tool Selection",
            text="Select a tool:",
            options=tool_options,
            descriptions=tool_descriptions,
            default=tool_options[0],
        )
        if tool:
            return int(tool)
    return None

def selectEnabledTool() -> Optional[str]:
    tool_options = []
    tool_descriptions = []
    for name in config.toolFunctionSchemas:
        tool_options.append(name)
        tool_descriptions.append(name.replace("_", " "))
    stopSpinning()
    tool = TerminalModeDialogs(None).getValidOptions(
        title="Tool Selection",
        text="Select a tool:",
        options=tool_options,
        descriptions=tool_descriptions,
        default=tool_options[0],
    )
    return tool

# connectivity

def isServerAlive(ip, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)  # Timeout in case of server not responding
    try:
        sock.connect((ip, port))
        sock.close()
        return True
    except socket.error:
        return False

def isUrlAlive(url):
    #print(urllib.request.urlopen("https://letmedoit.ai").getcode())
    try:
        request = requests.get(url, timeout=5)
    except:
        return False
    return True if request.status_code == 200 else False

def is_valid_url(url: str) -> bool:
    # Regular expression pattern for URL validation
    pattern = re.compile(
        r'^(http|https)://'  # http:// or https://
        r'([a-zA-Z0-9.-]+)'  # domain name
        r'(\.[a-zA-Z]{2,63})'  # dot and top-level domain (e.g. .com, .org)
        r'(:[0-9]{1,5})?'  # optional port number
        r'(/.*)?$'  # optional path
    )
    return bool(re.match(pattern, url))

# files

def isExistingPath(docs_path):
    # handle document path dragged to the terminal
    docs_path = docs_path.strip()
    search_replace = (
        ("^'(.*?)'$", r"\1"),
        ('^(File|Folder): "(.*?)"$', r"\2"),
    )
    for search, replace in search_replace:
        docs_path = re.sub(search, replace, docs_path)
    if "\\ " in docs_path or "\(" in docs_path:
        search_replace = (
            ("\\ ", " "),
            ("\(", "("),
        )
        for search, replace in search_replace:
            docs_path = docs_path.replace(search, replace)
    return docs_path if os.path.exists(os.path.expanduser(docs_path)) else ""

def getUnstructuredFiles(dir_path: str) -> list:
    full_paths = []
    for dirpath, _, files in os.walk(dir_path):
        for filename in files:
            _, file_extension = os.path.splitext(filename)
            if file_extension[1:] in TEXT_FORMATS:
                filepath = os.path.join(dirpath, filename)
                full_paths.append(filepath)
    return full_paths

def getFilenamesWithoutExtension(dir, ext):
    # Note: pathlib.Path(file).stem does not work with file name containg more than one dot, e.g. "*.db.sqlite"
    #files = glob.glob(os.path.join(dir, "*.{0}".format(ext)))
    #return sorted([file[len(dir)+1:-(len(ext)+1)] for file in files if os.path.isfile(file)])
    return sorted([f[:-(len(ext)+1)] for f in os.listdir(dir) if f.lower().endswith(f".{ext}") and os.path.isfile(os.path.join(dir, f))])

def getLocalStorage():
    # config.freeGeniusAIName
    if not hasattr(config, "freeGeniusAIName") or not config.freeGeniusAIName:
        config.freeGeniusAIName = "FreeGenius AI"

    # option 1: config.storagedirectory; user custom folder
    if not hasattr(config, "storagedirectory") or (config.storagedirectory and not os.path.isdir(config.storagedirectory)):
        config.storagedirectory = ""
    if config.storagedirectory:
        return config.storagedirectory
    # option 2: defaultStorageDir; located in user home directory
    defaultStorageDir = os.path.join(os.path.expanduser('~'), config.freeGeniusAIName.split()[0].lower())
    try:
        Path(defaultStorageDir).mkdir(parents=True, exist_ok=True)
    except:
        pass
    if os.path.isdir(defaultStorageDir):
        return defaultStorageDir
    # option 3: directory "files" in app directory; to be deleted on every upgrade
    else:
        return os.path.join(config.freeGeniusAIFolder, "files")

# image

def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        base64_image = base64.b64encode(image_file.read()).decode('utf-8')
    ext = os.path.splitext(os.path.basename(image_path))[1][1:]
    return f"data:image/{ext};base64,{base64_image}"

def is_valid_image_url(url): 
    try: 
        response = requests.head(url, timeout=30)
        content_type = response.headers['content-type'] 
        if 'image' in content_type: 
            return True 
        else: 
            return False 
    except requests.exceptions.RequestException: 
        return False

def is_valid_image_file(file_path):
    try:
        # Open the image file
        with Image.open(file_path) as img:
            # Check if the file format is supported by PIL
            img.verify()
            return True
    except (IOError, SyntaxError) as e:
        # The file path is not a valid image file path
        return False

# call llm

def executeToolFunction(func_arguments: dict, function_name: str):
    def notifyDeveloper(func_name):
        if config.developer:
            #print1(f"running function '{func_name}' ...")
            print_formatted_text(HTML(f"<{config.terminalPromptIndicatorColor2}>Running function</{config.terminalPromptIndicatorColor2}> <{config.terminalCommandEntryColor2}>'{func_name}'</{config.terminalCommandEntryColor2}> <{config.terminalPromptIndicatorColor2}>...</{config.terminalPromptIndicatorColor2}>"))
    if not function_name in config.toolFunctionMethods:
        if config.developer:
            print1(f"Unexpected function: {function_name}")
            print1(config.divider)
            print(func_arguments)
            print1(config.divider)
        function_response = "[INVALID]"
    else:
        notifyDeveloper(function_name)
        function_response = config.toolFunctionMethods[function_name](func_arguments)
    return function_response

def toParameterSchema(schema) -> dict:
    """
    extract parameter schema from full schema
    """
    if "parameters" in schema:
        return schema["parameters"]
    return schema

def toChatml(messages: dict=[], use_system_message=True) -> str:
    messages_str = ""
    roles = {
        "user": "<|im_start|>user\n{content}\n<|im_end|>\n",
        "assistant": "<|im_start|>assistant\n{content}\n<|im_end|>\n",
    }
    if use_system_message:
        roles["system"] = "<|im_start|>system\n{content}\n<|im_end|>\n"
    for message in messages:
        role, content = message.get("role", ""), message.get("content", "")
        if role and role in roles and content:
            messages_str += roles[role].format(content=content)
    return messages_str.rstrip()

def toGeminiMessages(messages: dict=[]) -> Optional[list]:
    systemMessage = ""
    lastUserMessage = ""
    if messages:
        history = []
        for i in messages:
            role = i.get("role", "")
            content = i.get("content", "")
            if role in ("user", "assistant"):
                history.append(Content(role="user" if role == "user" else "model", parts=[Part.from_text(content)]))
                if role == "user":
                    lastUserMessage = content
            elif role == "system":
                systemMessage = content
        if history and history[-1].role == "user":
            history = history[:-1]
        else:
            lastUserMessage = ""
        if not history:
            history = None
    else:
        history = None
    return history, systemMessage, lastUserMessage

# python code

def execPythonFile(script="", content=""):
    if script or content:
        try:
            def runCode(text):
                code = compile(text, script, 'exec')
                exec(code, globals())
            if content:
                runCode(content)
            else:
                with open(script, 'r', encoding='utf8') as f:
                    runCode(f.read())
            return True
        except:
            print1("Failed to run '{0}'!".format(os.path.basename(script)))
            showErrors()
    return False

def isValidPythodCode(code):
    try:
        codeObject = compile(code, '<string>', 'exec')
        return codeObject
    except:
        return None

def extractPythonCode(content, keepInvalid=False):
    content = content.replace("<python>", "")
    content = content.replace("</python>", "")
    content = content.replace("<\/python>", "")
    content = re.sub("^python[ ]*\n", "", content).strip()
    content = re.sub("^```.*?\n", "", content, flags=re.M).strip()
    content = re.sub("\n```.*?$", "", content, flags=re.M).strip()
    if code_only := re.search('```python[ ]*\n(.+?)```', content, re.DOTALL):
        content = code_only.group(1).strip()
    elif code_only := re.search('```[ ]*\n(.+?)```', content, re.DOTALL):
        content = code_only.group(1).strip()
    content = re.sub("\n```[^\n]*?$", "", content, flags=re.M)
    content = re.sub("^<[^<>]*?>", "", content)
    content = re.sub("<[^<>]*?>$", "", content)
    if keepInvalid or isValidPythodCode(content) is not None:
        config.pagerContent = f'''```python
{content}```'''
        return content
    return ""

def fineTunePythonCode(code):
    # dedent
    code = textwrap.dedent(code).rstrip()
    code = re.sub("^python[ ]*\n", "", code)
    # extract from code block, if any
    if code_only := re.search('```python\n(.+?)```', code, re.DOTALL):
        code = code_only.group(1).strip()
    # make sure it is run as main program
    if "\nif __name__ == '__main__':\n" in code:
        code, main = code.split("\nif __name__ == '__main__':\n", 1)
        code = code.strip()
        main = "\n" + textwrap.dedent(main)
    elif '\nif __name__ == "__main__":\n' in code:
        code, main = code.split('\nif __name__ == "__main__":\n', 1)
        code = code.strip()
        main = "\n" + textwrap.dedent(main)
    else:
        main = ""
    # capture print output
    config.pythonFunctionResponse = ""
    insert_string = "from freegenius import config\nconfig.pythonFunctionResponse = "
    code = re.sub("^!(.*?)$", r'import os\nos.system(""" \1 """)', code, flags=re.M)
    if "\n" in code:
        substrings = code.rsplit("\n", 1)
        lastLine = re.sub("print\((.*)\)", r"\1", substrings[-1])
        if lastLine.startswith(" "):
            lastLine = re.sub("^([ ]+?)([^ ].*?)$", r"\1config.pythonFunctionResponse = \2", lastLine)
            code = f"from freegenius import config\n{substrings[0]}\n{lastLine}"
        else:
            lastLine = f"{insert_string}{lastLine}"
            code = f"{substrings[0]}\n{lastLine}"
    else:
        code = f"{insert_string}{code}"
    return f"{code}{main}"

def getPythonFunctionResponse(code):
    #return str(config.pythonFunctionResponse) if config.pythonFunctionResponse is not None and (type(config.pythonFunctionResponse) in (int, float, str, list, tuple, dict, set, bool) or str(type(config.pythonFunctionResponse)).startswith("<class 'numpy.")) and not ("os.system(" in code) else ""
    return "" if config.pythonFunctionResponse is None else str(config.pythonFunctionResponse)

def showRisk(risk):
    if not config.confirmExecution in ("always", "medium_risk_or_above", "high_risk_only", "none"):
        config.confirmExecution = "always"
    print1(f"[risk level: {risk}]")

def confirmExecution(risk):
    if config.confirmExecution == "always" or (risk == "high" and config.confirmExecution == "high_risk_only") or (not risk == "low" and config.confirmExecution == "medium_risk_or_above"):
        return True
    else:
        return False

# embedding

def getEmbeddingFunction(embeddingModel=None):
    # import statement is placed here to make this file compatible on Android
    embeddingModel = embeddingModel if embeddingModel is not None else config.embeddingModel
    if embeddingModel in ("text-embedding-3-large", "text-embedding-3-small", "text-embedding-ada-002"):
        return embedding_functions.OpenAIEmbeddingFunction(api_key=config.openaiApiKey, model_name=embeddingModel)
    return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=embeddingModel) # support custom Sentence Transformer Embedding models by modifying config.embeddingModel

# chromadb

def get_or_create_collection(client, collection_name):
    collection = client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
        embedding_function=getEmbeddingFunction(),
    )
    return collection

def add_vector(collection, text, metadata):
    id = str(uuid.uuid4())
    collection.add(
        documents = [text],
        metadatas = [metadata],
        ids = [id]
    )

def query_vectors(collection, query, n=1):
    return collection.query(
        query_texts=[query],
        n_results = n,
    )

# spinning

def spinning_animation(stop_event):
    while not stop_event.is_set():
        for symbol in "|/-\\":
            print(symbol, end="\r")
            time.sleep(0.1)

def startSpinning():
    config.stop_event = threading.Event()
    config.spinner_thread = threading.Thread(target=spinning_animation, args=(config.stop_event,))
    config.spinner_thread.start()

def stopSpinning():
    try:
        config.stop_event.set()
        config.spinner_thread.join()
    except:
        pass

# display information

def wrapText(content, terminal_width=None):
    if terminal_width is None:
        terminal_width = shutil.get_terminal_size().columns
    return "\n".join([textwrap.fill(line, width=terminal_width) for line in content.split("\n")])

def transformText(text):
    for transformer in config.outputTransformers:
            text = transformer(text)
    return text

def print1(content):
    content = transformText(content)
    if config.wrapWords:
        # wrap words to fit terminal width
        terminal_width = shutil.get_terminal_size().columns
        print(wrapText(content, terminal_width))
        # remarks: 'fold' or 'fmt' does not work on Windows
        # pydoc.pipepager(f"{content}\n", cmd=f"fold -s -w {terminal_width}")
        # pydoc.pipepager(f"{content}\n", cmd=f"fmt -w {terminal_width}")
    else:
        print(content)

def print2(content):
    print_formatted_text(HTML(f"<{config.terminalPromptIndicatorColor2}>{content}</{config.terminalPromptIndicatorColor2}>"))

def print3(content):
    splittedContent = content.split(": ", 1)
    if len(splittedContent) == 2:
        key, value = splittedContent
        print_formatted_text(HTML(f"<{config.terminalPromptIndicatorColor2}>{key}:</{config.terminalPromptIndicatorColor2}> {value}"))
    else:
        print2(splittedContent)

def getStringWidth(text):
    width = 0
    for character in text:
        width += wcwidth.wcwidth(character)
    return width

def getPygmentsStyle():
    theme = config.pygments_style if config.pygments_style else "stata-dark" if not config.terminalResourceLinkColor.startswith("ansibright") else "stata-light"
    return style_from_pygments_cls(get_style_by_name(theme))

def showErrors():
    trace = traceback.format_exc()
    print(trace if config.developer else "Error encountered!")
    return trace

def check_llm_errors(func):
    """A decorator that handles llm exceptions for the function it wraps."""
    def wrapper(*args, **kwargs):
        def finishError():
            config.stopSpinning()
            return "[INVALID]"
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_message = f"An error occurred in {func.__name__}: {e}"
            error_traceback = traceback.format_exc()
            print(error_message)
            print(error_traceback)

            return finishError()
    return wrapper

# online

def get_wan_ip():
    try:
        response = requests.get('https://api.ipify.org?format=json', timeout=5)
        data = response.json()
        return data['ip']
    except:
        return ""

def get_local_ip():
    interfaces = netifaces.interfaces()
    for interface in interfaces:
        addresses = netifaces.ifaddresses(interface)
        if netifaces.AF_INET in addresses:
            for address in addresses[netifaces.AF_INET]:
                ip = address['addr']
                if ip != '127.0.0.1':
                    return ip

def runSystemCommand(command):
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    output = result.stdout  # Captured standard output
    error = result.stderr  # Captured standard error
    response = ""
    response += f"# Output:\n{output}"
    if error.strip():
        response += f"\n# Error:\n{error}"
    return response

def openURL(url):
    config.stopSpinning()
    if config.terminalEnableTermuxAPI:
        command = f'''termux-open-url "{url}"'''
        runSystemCommand(command)
    else:
        webbrowser.open(url)

def getWebText(url):
    try:
        # Download webpage content
        response = requests.get(url, timeout=30)
        # Parse the HTML content to extract text
        soup = BeautifulSoup(response.text, 'html.parser')
        return soup.get_text()
    except:
        return ""

def downloadWebContent(url, timeout=60, folder="", ignoreKind=False):
    print2("Downloading web content ...")
    hasExt = re.search("\.([^\./]+?)$", url)
    supported_documents = TEXT_FORMATS[:]
    supported_documents.remove("org")

    response = requests.get(url, timeout=timeout)
    folder = folder if folder and os.path.isdir(folder) else os.path.join(config.freeGeniusAIFolder, "temp")
    filename = quote(url, safe="")
    def downloadBinary(filename=filename):
        filename = os.path.join(folder, filename)
        with open(filename, "wb") as fileObj:
            fileObj.write(response.content)
        return filename
    def downloadHTML(filename=filename):
        filename = os.path.join(folder, f"{filename}.html")
        with open(filename, "w", encoding="utf-8") as fileObj:
            fileObj.write(response.text)
        return filename

    try:
        if ignoreKind:
            filename = downloadBinary()
            print3(f"Downloaded at: {filename}")
            return ("any", filename)
        elif hasExt and hasExt.group(1) in supported_documents:
            return ("document", downloadBinary())
        elif is_valid_image_url(url):
            return ("image", downloadBinary())
        else:
            # download content as text
            # Save the content to a html file
            return ("text", downloadHTML())
    except:
        showErrors()
        return ("", "")

# time

def getCurrentDateTime():
    current_datetime = datetime.datetime.now()
    return current_datetime.strftime("%Y-%m-%d_%H_%M_%S")

def addTimeStamp(content):
    time = re.sub("\.[^\.]+?$", "", str(datetime.datetime.now()))
    return f"{content}\n[Current time: {time}]"

def getDayOfWeek():
    if config.isTermux:
        return ""
    else:
        now = pendulum.now() 
        return now.format('dddd')

# device information

def getDeviceInfo(includeIp=False):
    g = geocoder.ip('me')
    if hasattr(config, "thisPlatform"):
        thisPlatform = config.thisPlatform
    else:
        thisPlatform = platform.system()
        if thisPlatform == "Darwin":
            thisPlatform = "macOS"
    if config.includeIpInDeviceInfoTemp or includeIp or (config.includeIpInDeviceInfo and config.includeIpInDeviceInfoTemp):
        wan_ip = get_wan_ip()
        local_ip = get_local_ip()
        ipInfo = f'''Wan ip: {wan_ip}
Local ip: {local_ip}
'''
    else:
        ipInfo = ""
    if config.isTermux:
        dayOfWeek = ""
    else:
        dayOfWeek = getDayOfWeek()
        dayOfWeek = f"Current day of the week: {dayOfWeek}"
    return f"""Operating system: {thisPlatform}
Version: {platform.version()}
Machine: {platform.machine()}
Architecture: {platform.architecture()[0]}
Processor: {platform.processor()}
Hostname: {socket.gethostname()}
Username: {getpass.getuser()}
Python version: {platform.python_version()}
Python implementation: {platform.python_implementation()}
Current directory: {os.getcwd()}
Current time: {str(datetime.datetime.now())}
{dayOfWeek}
{ipInfo}Latitude & longitude: {g.latlng}
Country: {g.country}
State: {g.state}
City: {g.city}"""

# token management

# token limit
# reference: https://platform.openai.com/docs/models/gpt-4
tokenLimits = {
    "gpt-4o": 128000,
    "gpt-4-turbo": 128000, # Returns a maximum of 4,096 output tokens.
    "gpt-4-turbo-preview": 128000, # Returns a maximum of 4,096 output tokens.
    "gpt-4-0125-preview": 128000, # Returns a maximum of 4,096 output tokens.
    "gpt-4-1106-preview": 128000, # Returns a maximum of 4,096 output tokens.
    "gpt-3.5-turbo": 16385, # Returns a maximum of 4,096 output tokens.
    "gpt-3.5-turbo-16k": 16385,
    "gpt-4": 8192,
    "gpt-4-32k": 32768,
}

def getDynamicTokens(messages, functionSignatures=None):
    if functionSignatures is None:
        functionTokens = 0
    else:
        functionTokens = count_tokens_from_functions(functionSignatures)
    tokenLimit = tokenLimits[config.chatGPTApiModel]
    currentMessagesTokens = count_tokens_from_messages(messages) + functionTokens
    availableTokens = tokenLimit - currentMessagesTokens
    if availableTokens >= config.chatGPTApiMaxTokens:
        return config.chatGPTApiMaxTokens
    elif (config.chatGPTApiMaxTokens > availableTokens > config.chatGPTApiMinTokens):
        return availableTokens
    return config.chatGPTApiMinTokens

def count_tokens_from_functions(functionSignatures, model=""):
    count = 0
    if not model:
        model = config.chatGPTApiModel
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        print("Warning: model not found. Using cl100k_base encoding.")
        encoding = tiktoken.get_encoding("cl100k_base")
    for i in functionSignatures:
        count += len(encoding.encode(str(i)))
    return count

# The following method was modified from source:
# https://github.com/openai/openai-cookbook/blob/main/examples/How_to_count_tokens_with_tiktoken.ipynb
def count_tokens_from_messages(messages, model=""):
    if not model:
        model = config.chatGPTApiModel

    """Return the number of tokens used by a list of messages."""
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        print("Warning: model not found. Using cl100k_base encoding.")
        encoding = tiktoken.get_encoding("cl100k_base")
    if model in {
            "gpt-4o",
            "gpt-3.5-turbo",
            "gpt-3.5-turbo-0125",
            "gpt-3.5-turbo-1106",
            "gpt-3.5-turbo-0613",
            "gpt-3.5-turbo-16k",
            "gpt-3.5-turbo-16k-0613",
            "gpt-4-turbo",
            "gpt-4-turbo-preview",
            "gpt-4-0125-preview",
            "gpt-4-1106-preview",
            "gpt-4-0314",
            "gpt-4-32k-0314",
            "gpt-4",
            "gpt-4-0613",
            "gpt-4-32k",
            "gpt-4-32k-0613",
        }:
        tokens_per_message = 3
        tokens_per_name = 1
    elif model == "gpt-3.5-turbo-0301":
        tokens_per_message = 4  # every message follows <|start|>{role/name}\n{content}<|end|>\n
        tokens_per_name = -1  # if there's a name, the role is omitted
    elif "gpt-3.5-turbo" in model:
        #print("Warning: gpt-3.5-turbo may update over time. Returning num tokens assuming gpt-3.5-turbo-0613.")
        return count_tokens_from_messages(messages, model="gpt-3.5-turbo-0613")
    elif "gpt-4" in model:
        #print("Warning: gpt-4 may update over time. Returning num tokens assuming gpt-4-0613.")
        return count_tokens_from_messages(messages, model="gpt-4-0613")
    else:
        raise NotImplementedError(
            f"""count_tokens_from_messages() is not implemented for model {model}. See https://github.com/openai/openai-python/blob/main/chatml.md for information on how messages are converted to tokens."""
        )
    num_tokens = 0
    for message in messages:
        num_tokens += tokens_per_message
        if not "content" in message or not message.get("content", ""):
            num_tokens += len(encoding.encode(str(message)))
        else:
            for key, value in message.items():
                num_tokens += len(encoding.encode(value))
                if key == "name":
                    num_tokens += tokens_per_name
    num_tokens += 3  # every reply is primed with <|start|>assistant<|message|>
    return num_tokens

# API keys / credentials

def changeChatGPTAPIkey():
    print("Enter your OpenAI API Key [optional]:")
    apikey = prompt(default=config.openaiApiKey, is_password=True)
    if apikey and not apikey.strip().lower() in (config.cancel_entry, config.exit_entry):
        config.openaiApiKey = apikey
    else:
        config.openaiApiKey = "freegenius"
    setChatGPTAPIkey()

def setChatGPTAPIkey():
    # instantiate a client that can shared with plugins
    os.environ["OPENAI_API_KEY"] = config.openaiApiKey
    config.oai_client = OpenAI()
    # set variable 'OAI_CONFIG_LIST' to work with pyautogen
    oai_config_list = []
    for model in tokenLimits.keys():
        oai_config_list.append({"model": model, "api_key": config.openaiApiKey})
    os.environ["OAI_CONFIG_LIST"] = json.dumps(oai_config_list)

def setGoogleCredentials():
    config.google_cloud_credentials_file = os.path.join(config.localStorage, "credentials_google_cloud.json")
    if config.google_cloud_credentials and os.path.isfile(config.google_cloud_credentials):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = config.google_cloud_credentials
    else:
        gccfile2 = os.path.join(config.localStorage, "credentials_googleaistudio.json")
        gccfile3 = os.path.join(config.localStorage, "credentials_googletts.json")

        if os.path.isfile(config.google_cloud_credentials_file):
            config.google_cloud_credentials = config.google_cloud_credentials_file
        elif os.path.isfile(gccfile2):
            config.google_cloud_credentials = gccfile2
        elif os.path.isfile(gccfile3):
            config.google_cloud_credentials = gccfile3
        else:
            config.google_cloud_credentials = ""
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = config.google_cloud_credentials if config.google_cloud_credentials else ""

# real-time information

def getWeather(latlng=""):
    # get current weather information
    # Reference: https://openweathermap.org/api/one-call-3

    if not config.openweathermapApi:
        return None

    # latitude, longitude
    if not latlng:
        latlng = geocoder.ip('me').latlng

    try:
        latitude, longitude = latlng
        # Build the URL for the weather API
        url = f"https://api.openweathermap.org/data/2.5/weather?lat={latitude}&lon={longitude}&appid={config.openweathermapApi}&units=metric"
        # Make the request to the API
        response = requests.get(url)
        # Parse the JSON response
        data = json.loads(response.text)
        # Get the current weather condition
        weather_condition = data["weather"][0]["description"]
        # Get the current temperature in Celsius
        temperature_celsius = data["main"]["temp"]

        # Convert the temperature to Fahrenheit
        #temperature_fahrenheit = (temperature_celsius * 9/5) + 32

        # Print the weather condition and temperature
        #print(f"The current weather condition is {weather_condition}.")
        #print(f"The current temperature is {temperature_fahrenheit} degrees Fahrenheit.")
        return temperature_celsius, weather_condition
    except:
        showErrors()
        return None

# package management

def isCommandInstalled(package):
    return True if shutil.which(package.split(" ", 1)[0]) else False

def getPackageInstalledVersion(package):
    try:
        installed_version = pkg_resources.get_distribution(package).version
        return version.parse(installed_version)
    except pkg_resources.DistributionNotFound:
        return None

def getPackageLatestVersion(package):
    try:
        response = requests.get(f"https://pypi.org/pypi/{package}/json", timeout=10)
        latest_version = response.json()['info']['version']
        return version.parse(latest_version)
    except:
        return None

def restartApp():
    print(f"Restarting {config.freeGeniusAIName} ...")
    os.system(f"{sys.executable} {config.freeGeniusAIFile}")
    exit(0)

def updateApp():
    package = os.path.basename(config.freeGeniusAIFolder)
    thisPackage = f"{package}_android" if config.isTermux else package
    print(f"Checking '{thisPackage}' version ...")
    installed_version = getPackageInstalledVersion(thisPackage)
    if installed_version is None:
        print("Installed version information is not accessible!")
    else:
        print(f"Installed version: {installed_version}")
    latest_version = getPackageLatestVersion(thisPackage)
    if latest_version is None:
        print("Latest version information is not accessible at the moment!")
    elif installed_version is not None:
        print(f"Latest version: {latest_version}")
        if latest_version > installed_version:
            if config.thisPlatform == "Windows":
                print("Automatic upgrade feature is yet to be supported on Windows!")
                print(f"Run 'pip install --upgrade {thisPackage}' to manually upgrade this app!")
            else:
                try:
                    # upgrade package
                    installPipPackage(f"--upgrade {thisPackage}")
                    restartApp()
                except:
                    if config.developer:
                        print(traceback.format_exc())
                    print(f"Failed to upgrade '{thisPackage}'!")

def installPipPackage(module, update=True):
    #executablePath = os.path.dirname(sys.executable)
    #pippath = os.path.join(executablePath, "pip")
    #pip = pippath if os.path.isfile(pippath) else "pip"
    #pip3path = os.path.join(executablePath, "pip3")
    #pip3 = pip3path if os.path.isfile(pip3path) else "pip3"

    if isCommandInstalled("pip"):
        pipInstallCommand = f"{sys.executable} -m pip install"

        if update:
            if not config.isPipUpdated:
                pipFailedUpdated = "pip tool failed to be updated!"
                try:
                    # Update pip tool in case it is too old
                    updatePip = subprocess.Popen(f"{pipInstallCommand} --upgrade pip", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    *_, stderr = updatePip.communicate()
                    if not stderr:
                        print("pip tool updated!")
                    else:
                        print(pipFailedUpdated)
                except:
                    print(pipFailedUpdated)
                config.isPipUpdated = True
        try:
            upgrade = (module.startswith("-U ") or module.startswith("--upgrade "))
            if upgrade:
                moduleName = re.sub("^[^ ]+? (.+?)$", r"\1", module)
            else:
                moduleName = module
            print(f"{'Upgrading' if upgrade else 'Installing'} '{moduleName}' ...")
            installNewModule = subprocess.Popen(f"{pipInstallCommand} {module}", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            *_, stderr = installNewModule.communicate()
            if not stderr:
                print(f"Package '{moduleName}' {'upgraded' if upgrade else 'installed'}!")
            else:
                print(f"Failed {'upgrading' if upgrade else 'installing'} package '{moduleName}'!")
                if config.developer:
                    print(stderr)
            return True
        except:
            return False

    else:
        print("pip command is not found!")
        return False

# config

def toggleinputaudio():
    #if self.isTtsAvailable:
    config.ttsInput = not config.ttsInput
    config.saveConfig()
    print3(f"Input Audio: '{'enabled' if config.ttsInput else 'disabled'}'!")

def toggleoutputaudio():
    #if self.isTtsAvailable:
    config.ttsOutput = not config.ttsOutput
    config.saveConfig()
    print3(f"Output Audio: '{'enabled' if config.ttsOutput else 'disabled'}'!")

def setToolDependence(entry: Any) -> bool:
    """
    A quick way to change config.tool_dependence and config.tool_auto_selection_threshold
    """
    try:
        splits = entry.split("!", 1)
        if len(splits) == 2:
            tool_dependence, tool_auto_selection_threshold = splits
        else:
            tool_dependence = entry
            tool_auto_selection_threshold = None
        tool_dependence = float(tool_dependence)
        if 0 <= tool_dependence <=1.0:
            config.tool_dependence = tool_dependence
            print3(f"Tool dependence changed to: {tool_dependence}")

            if tool_auto_selection_threshold is not None:
                tool_auto_selection_threshold = float(tool_auto_selection_threshold)
                if 0 <= tool_auto_selection_threshold <=1.0:
                    config.tool_auto_selection_threshold = tool_auto_selection_threshold
            else:
                # 3/4 of config.tool_dependence
                config.tool_auto_selection_threshold = round(config.tool_dependence * 5/8, 5)
            print3(f"Tool auto selection threshold changed to: {config.tool_auto_selection_threshold}")

            config.saveConfig()

            return True
    except:
        pass
    return False