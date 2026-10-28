# Trade Area Lab

Demonstração: https://trade-area-lab.onrender.com/

Pacote público da interface React/Leaflet e API Python, com postos ANP e transações simuladas. Busca por nome, CNPJ, código, endereço, bairro e CEP, seleção explícita de origem/destino e recálculo de cenários.

O mapa consulta até 1.000 posições utilizáveis por filtro, independentemente da página da tabela; contagens e truncagem são explícitas. Um posto selecionado adicional pode aparecer além do conjunto do mapa.

Ruas são fornecidas diretamente ao navegador por https://tile.openstreetmap.org, com atribuição © OpenStreetMap contributors e cache/Referer normais. Sem chave, proxy ou download em lote. O botão Mapa local usa a cartografia vetorial salva quando a camada externa estiver indisponível. A camada de ruas depende de internet e da política do provedor.

O modelo Python e a distância geográfica são locais ao serviço. O roteador viário não está hospedado: /api/route responde 503 explícito; GraphHopper permanece na instalação local separada.

Render Free, um worker, sem banco/disco adicional e deploy automático desligado. O workspace verificado é Hobby sem cartão. Limites/inatividade podem suspender a instância. Instalar com pip install -r requirements.txt e executar uvicorn app:app --host 0.0.0.0 --port $PORT --workers 1. Python3.12 é selecionado por .python-version.

Somente este pacote sanitizado deve ser publicado. Não contém credenciais, documentos, histórico Git corporativo ou transações reais. deployment-manifest.json lista hashes dos arquivos, excluindo seu próprio conteúdo. vercel.json é metadado inerte da preparação original.
