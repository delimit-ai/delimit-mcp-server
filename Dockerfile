FROM node:20-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip && rm -rf /var/lib/apt/lists/*
RUN pip3 install --break-system-packages pyyaml pydantic packaging
WORKDIR /app
COPY package*.json ./
RUN npm install --production --ignore-scripts
COPY . .
ENV DELIMIT_GATEWAY_ROOT=/app/gateway
ENTRYPOINT ["node", "bin/delimit-cli.js"]
