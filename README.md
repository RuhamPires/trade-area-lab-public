# Trade Area Lab no Render

Pacote sanitizado da demonstração com cadastro público de postos da ANP e transações simuladas. Contém a interface compilada e a API Python existentes. O pacote foi preparado localmente; a publicação e o acesso na rede corporativa ainda precisam ser verificados.

Configure um serviço web Python com o arquivo `render.yaml`, usando o plano **Free**, sem banco, disco pago ou implantação automática. A versão do Python é selecionada pelo arquivo `.python-version`. Instalação: `pip install -r requirements.txt`. Inicialização: `uvicorn app:app --host 0.0.0.0 --port $PORT --workers 1`.

O serviço serve interface, arquivos estáticos e API no mesmo endereço. A verificação de saúde fica em `/api/health`. O catálogo público está incluído em `web/data`; não é necessário consultar ANP ou serviços corporativos durante a apresentação. O arquivo `vercel.json` permanece como metadado inerte da preparação original.

Distâncias geográficas e recálculo de cenários usam o motor Python incluído. O roteamento por estrada exige outro motor e permanece não configurado nesta publicação; `/api/route` retorna erro explícito de indisponibilidade. Não definir endereço de roteador externo para a máquina local.

Publique somente esta pasta sanitizada. Não acrescente arquivos privados, credenciais, histórico Git do projeto original, PDFs, transcrição, dados corporativos ou mapas brutos. `deployment-manifest.json` contém a lista de arquivos com seus hashes e exclui seu próprio conteúdo do cálculo.

O plano gratuito tem limites e pode suspender o serviço por inatividade. O endereço novo também depende das políticas de acesso da organização. Não configure plano pago ou método de pagamento como parte desta preparação.
