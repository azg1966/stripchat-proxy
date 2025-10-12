# Stripchat Proxy
Its copy of [cp-standalone](https://github.com/aitschti/scp-standalone) ported to [AioHTTP](https://github.com/aio-libs/aiohttp) framework.

## Install (in Linux)
Clone this repo  
```cmd
$ git clone https://github.com/azg1966/stripchat-proxy.git  
```
cd to cloned path  
```cmd
$ cd stripchat-proxy  
```
Create virtual environment  
```cmd
$ python -m venv ./env  
```
Activate it  
```cmd
$ source ./env/bin/activate  
```
Install dependencies  
```cmd
$ pip install -r requirements.txt  
```

## Usage

### Run proxy
Go to cloned path, activate virtual environment and run proxy:
```cmd
$ python stripchat_proxy.py --port=9999 --host=127.0.0.1  
```

All available options:
```cmd
-h, --help            show help message and exit
--host HOST           Host to bind to (default: 127.0.0.1)
--port PORT           Port to run the proxy on (default: auto)
--geo-bypass-proxy GEO_BYPASS_PROXY Proxy to bypass cuntry ban by GeoIP
--stripchat-host STRIPCHAT_HOST Optional mirror domain for stripchat, like 'omg.adult'
```
#### Censorship bypass
If stripchat.com domain blacklisted by your provider you can change mirror (superchat.live or omg.adult ... google it) with 
```cmd
--stripchat-host
```
 option:


```cmd
python stripchat_proxy.py --port=9999 --host=127.0.0.1 --stripchat-host=omg.adult
```

or/and if your country blocked by model you can use proxy server:
##### SOCKS5:
```cmd
python stripchat_proxy.py --geo-bypass-proxy=socks5://addr:port --stripchat-host=omg.adult
```
#### HTTP:
```cmd
python stripchat_proxy.py --geo-bypass-proxy=http://addr:port --stripchat-host=omg.adult
````

### Access to stream
Use `http://<host>:<port>/<model_name>` to fetch and stream for that model.
