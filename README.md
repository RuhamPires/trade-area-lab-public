# Trade Area Lab

Demonstração: https://trade-area-lab.onrender.com/

Interface React/Leaflet e API Python com cadastro público ANP e transações simuladas. Busca por nome, CNPJ, código, endereço, bairro e CEP, mapa de até 1.000 posições por filtro, origem/destino e recálculo de cenários.

Iniciar tour executa 13 etapas reais: busca, distância/raio, coordenadas, percurso viário, dois cenários, comparação e prévias CSV/JSON. Sua sessão permanece preservada e é restaurada ao sair. Nenhum download automático.

Valhalla 3.9.1 executa dentro do mesmo serviço, com grafo OpenStreetMap Campinas–Jundiaí. A API devolve geometria real, km e minutos estimados para carro sem trânsito em tempo real. Fora da cobertura retorna erro explícito. Não usa Google nem roteadores públicos de demonstração. O mapa regional tem timestamp 26/10/2026; routing-public/manifest.json registra origem, cobertura e hash. ODbL 1.0 e © OpenStreetMap contributors: https://www.openstreetmap.org/copyright .

A camada visual de ruas consulta diretamente https://tile.openstreetmap.org com atribuição/cache/Referer normais, sem chave/proxy/download em lote. Mapa local e Somente percurso permitem visualizar os arquivos vetoriais salvos. Tiles externos dependem de internet e da política do provedor.

Render Free, um worker, sem banco/disco adicional e deploy automático desligado. O workspace verificado é Hobby sem cartão. Limites e inatividade podem suspender o serviço. Python 3.12; build: pip install -r requirements.txt && python build_routing.py . Start: uvicorn app:app --host 0.0.0.0 --port $PORT --workers 1 . Use os espaços mostrados em render.yaml para o comando exato.

O build gera tiles nativos Linux antes da promoção e exige smoke de rota, cobertura, perfil e modelo real de 1.000 postos/2.000 veículos simulados dentro do limite de memória/tempo. Não gere o grafo durante requisições nem copie tiles Windows. Rotas e modelos são serializados; concorrência pesada retorna 429.

Fontes abertas: https://github.com/valhalla/valhalla e https://github.com/nilbuild/driver.js . O modelo Ward/CC/CR mantém as fórmulas documentadas. Esta demonstração não comprova equivalência com o sistema corporativo; essa comparação requer dados anonimizados e validação do time.

Somente este pacote sanitizado deve ser publicado. Sem credenciais, documentos, histórico Git corporativo ou transações reais. deployment-manifest.json lista hashes dos arquivos, excluindo a si próprio. vercel.json é metadado inerte da preparação original.
