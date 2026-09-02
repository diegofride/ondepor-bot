# Dejar el panel OnDepor online (Oracle Cloud, gratis)

Objetivo: que el bot corra **24/7 en un servidor gratis** y lo manejes desde el
**navegador** (compu o celular), sin dejar tu PC prendida.

> Esto es la parte más técnica de todo el proyecto. Está pensado para hacerlo
> **juntos paso a paso** — decime "dale, deployemos" y te voy guiando en vivo.
> Igual acá queda todo escrito para copiar y pegar.

El motor y el panel ya están listos (`webapp.py`). Solo hay que ponerlos en un
servidor Linux siempre prendido.

---

## Parte 1 — Crear el servidor gratis en Oracle Cloud

1. Entrá a **https://www.oracle.com/cloud/free/** y creá una cuenta (pide una
   tarjeta para verificar identidad, **no cobra**; elegí los recursos "Always Free").
2. En la consola de Oracle: **Menu → Compute → Instances → Create Instance**.
3. Configurá:
   - **Image**: Ubuntu 22.04 (o 24.04).
   - **Shape**: elegí **VM.Standard.A1.Flex** (ARM) — es la que entra en el
     "Always Free" (podés poner 1 OCPU y 6 GB de RAM, sobra).
   - **SSH keys**: elegí "Generate a key pair for me" y **descargá la clave privada**
     (la vas a necesitar para entrar). Guardala bien.
4. **Create**. Cuando termine, anotá la **IP pública** de la instancia.
5. Abrí el puerto del panel: **Networking → Virtual Cloud Network → tu VCN →
   Security Lists → Default → Add Ingress Rule**:
   - Source CIDR: `0.0.0.0/0`  ·  IP Protocol: TCP  ·  Destination Port: `8080`.
   *(Si vas a usar Tailscale — ver Parte 4 — este paso NO hace falta y es más seguro.)*

---

## Parte 2 — Entrar al servidor e instalar todo

Desde tu PC (PowerShell) conectate por SSH (cambiá la ruta de la clave y la IP):

```
ssh -i ~/.ssh/tu-clave.key ubuntu@LA_IP_PUBLICA
```

Ya adentro del servidor, instalá Python y subí el código:

```bash
sudo apt update && sudo apt install -y python3 python3-pip git
mkdir -p ~/ondepor && cd ~/ondepor
```

**Subí los archivos de la carpeta `OndeporBot`** al servidor (a `~/ondepor`).
Dos formas fáciles:
- **Opción A (scp):** desde tu PC, en otra terminal:
  `scp -i ~/.ssh/clave.key -r ~/claude-mercadolibre/OndeporBot/* ubuntu@LA_IP:~/ondepor/`
- **Opción B (GitHub):** subimos la carpeta a un repo privado y hacés `git clone`.

Instalá las dependencias:

```bash
cd ~/ondepor
pip3 install -r requirements.txt
```

Poné **tu contraseña del panel** (la que vas a usar para entrar por el navegador):

```bash
nano webconfig.json      # cambiá "cambiame" por tu contraseña, guardá con Ctrl+O, Enter, Ctrl+X
```

Probá que arranca:

```bash
python3 webapp.py
```

En tu navegador andá a `http://LA_IP_PUBLICA:8080` → tenés que ver el login.
Frená con `Ctrl+C` (después lo dejamos corriendo solo en la Parte 3).

---

## Parte 3 — Dejarlo corriendo solo y para siempre (systemd)

Así el panel arranca solo y se reinicia si el servidor se reinicia:

```bash
sudo tee /etc/systemd/system/ondepor.service > /dev/null <<'EOF'
[Unit]
Description=Panel OnDepor
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/ondepor
ExecStart=/usr/bin/python3 /home/ubuntu/ondepor/webapp.py
Restart=always
Environment=PORT=8080

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ondepor
sudo systemctl status ondepor      # tiene que decir "active (running)"
```

Listo: el panel queda corriendo 24/7. Para ver los registros:
`journalctl -u ondepor -f`

> IMPORTANTE: dejá **una sola** instancia corriendo (así como está). No lo pongas
> con varios "workers" de gunicorn, porque el reloj interno se duplicaría y
> dispararía la reserva más de una vez.

---

## Parte 4 — Entrar de forma segura (elegí una)

Tal como quedó en la Parte 1, entrás por `http://LA_IP:8080`. Funciona, pero va
**sin HTTPS** (la contraseña viaja sin cifrar). Mejorá la seguridad con una de estas:

- **Tailscale (recomendada, gratis, sin dominio):** instala una red privada entre
  tus dispositivos y el servidor. Entrás desde tu celu/PC (con la app Tailscale)
  por una IP privada y con HTTPS, y **nadie más de internet puede entrar**.
  En el servidor: `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`.
  Instalás la app Tailscale en tu celu/PC con la misma cuenta y entrás a la IP que
  te da Tailscale. (En este caso, cerrá el puerto 8080 público de la Parte 1.)

- **Cloudflare Tunnel (URL pública con HTTPS):** te da una dirección `https://...`
  para entrar desde cualquier navegador sin instalar nada. Necesitás un dominio
  (~US$10/año) conectado a Cloudflare.

- **Rápido pero menos seguro:** dejarlo en `http://LA_IP:8080` y, como mínimo,
  poné una contraseña larga en `webconfig.json`.

---

## Recordá

- El servidor tiene su reloj sincronizado por internet, así que dispara al segundo
  exacto igual que en tu PC (y probablemente más rápido, por mejor conexión).
- Para actualizar el código: subís los archivos nuevos y `sudo systemctl restart ondepor`.
- Las reservas, jugadores y reservadores quedan guardados en los mismos archivos
  `.json` dentro de `~/ondepor`.
