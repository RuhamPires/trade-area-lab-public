"""Documented, executable defaults and presentation descriptions."""

from copy import deepcopy

from .contracts import NOTICE

LIMITS = {"stations": 1000, "vehicles": 2000, "max_events": 30, "total_events": 60000}
REFERENCE_DEFAULTS = {
    "max_passes": 8, "urban_ratio": 2, "road_ratio": 3,
    "candidates_road": 3, "candidates_urban": 1, "candidates_combined": 1,
    "road_distance_factor": 5, "distance_floor_km": 0.1,
    "transaction_percentile": 0.01, "urban_percentile": 0.85,
    "road_percentile": 0.8, "mixed_percentile": 0.7,
    "within_percentile": 0.9, "area_percentile": 0.97,
    "profile_urban_threshold": 0.8, "allocation_radius_km": 500,
    "exclude_full_overlap": False, "cross_uf": True, "ranking": "cc_per_km",
}
EXPLORATORY_DEFAULTS = {"target_size": 4, "max_passes": 8, "allocation_radius_km": 50.0}
CORRECTIONS = [
    "Ward utiliza projeção métrica local, preservando latitude e longitude originais.",
    "A diagonal de CC é excluída pela identidade; compartilhamento de 100% entre postos distintos permanece válido por padrão.",
    "CR é calculado para o próprio posto e os demais membros da área, com piso de distância declarado.",
    "Referência aplica decisões simultaneamente usando o início da iteração e recalcula métricas da composição final.",
    "Percentis contínuos usam interpolação linear e populações explícitas; distribuições vazias não se aplicam.",
    "Núcleo, padrões e centros são congelados antes da alocação final; cada posto aceito permanece uma vez no resultado.",
]


def _rule(identifier, title, description, category, formula=None):
    rule = {"id": identifier, "title": title, "description": description, "category": category}
    if formula:
        rule["formula"] = formula
    return rule


RULES = [
    _rule("synthetic_inputs", "Cadastro público e comportamento simulado", "Segmento operacional urbano/rodoviário, veículos e transações são artificiais e identificados por semente; o segmento público permanece não informado.", "dados"),
    _rule("eligibility_vehicle", "Elegibilidade do veículo", "Exige veículo válido, status 2, tipo 1, token presente e transação não cancelada. Campo desconhecido não aprova a transação.", "elegibilidade"),
    _rule("eligibility_station", "Elegibilidade do estabelecimento", "Exige estabelecimento não desativado, tipo 3, parceria Shell N, serviço 1 e faturamento diferente de N. A parceria não equivale à bandeira pública.", "elegibilidade"),
    _rule("operational_address", "Endereço e localização", "Usa endereço operacional tipo 10 ou endereço principal quando não existe tipo 10; exige localização urbana ou rodoviária.", "elegibilidade"),
    _rule("period", "Quatro meses completos", "Referência usa o primeiro dia do mês da data-base menos quatro meses até o último dia do mês anterior, inclusive. Exploratório usa janela em dias.", "periodo"),
    _rule("aggregates", "Agregados elegíveis", "Conta transações e veículos distintos e soma litros e valor autorizado após a janela e todos os filtros. Motivos de rejeição/desconhecidos são auditados.", "elegibilidade"),
    _rule("cc", "Compartilhamento direcional", "A diagonal é excluída por identidade. Compartilhamento de 100% entre postos distintos é válido; excluir 100% é opção de compatibilidade explícita.", "realocacao", "CC(i,j) = |V(i) ∩ V(j)| / |V(i)|, i ≠ j"),
    _rule("ward", "Grupos iniciais Ward", "Referencia agrupa por UF e segmento simulado antes do filtro de baixo volume, em projeção métrica local. Urbano usa razão 2 e rodoviário 3; grupos pequenos são válidos.", "agrupamento", "k = max(1, min(n, floor(n / razão)))"),
    _rule("low_volume", "Baixo volume", "P1 global é calculado entre postos com eventos elegíveis. Postos abaixo dele saem da realocação e permanecem para alocação final; postos sem eventos são variantes desde o início.", "percentis"),
    _rule("ranking", "Candidatos por posto", "Ordena por CC/km físico com piso 0,1 km e desempata por código. Considera 3 postos na rodada rodoviária e 1 nas demais; conta postos antes de deduplicar áreas.", "realocacao", "ranking(i,j) = CC(i,j) / max(km físico(i,j), 0,1)"),
    _rule("cr", "CR do posto avaliado", "Exclui o próprio posto da média. Grupo sem outro membro recebe CR zero; o CR do vizinho nunca substitui o do posto avaliado.", "realocacao", "CR(i,A) = média(CC(i,j)) / max(média(km(i,j)), 0,1), j ∈ A, j ≠ i"),
    _rule("rounds", "Rodoviária, urbana e conjunta", "Executa 8 iterações por rodada por padrão, incluindo zero mudanças. Primeiras rodadas preservam segmento; conjunta permite grupos mistos. Referência permite cruzar UFs.", "realocacao"),
    _rule("road_distance", "Ajuste rodoviário", "Na rodada conjunta km/5 para pares rodoviários entra no CR atual e candidato. Ranking conserva km físico; o ajuste não representa percurso viário.", "realocacao"),
    _rule("decision", "Decisão simultânea", "Cada iteração usa um snapshot; aplica todas as mudanças juntas, somente quando melhoria do CR próprio supera 1e-9. Empate mantém área; destinos empatados usam ID estável. Exploratório preserva decisões sequenciais e parada antecipada.", "realocacao"),
    _rule("profile", "Perfil da área", "Urbana quando participação urbana é estritamente maior que 80%; rodoviária quando não há urbanos; demais mistas, inclusive exatamente 80%.", "perfil"),
    _rule("outliers_profile", "Dispersão por perfil", "Compara médias de distância ajustada por TA com P85 urbano, P80 rodoviário e P70 misto entre TAs de cada perfil. Nas áreas acima do limite retira postos acima do P90 interno. Singletons não entram nas distribuições de distância.", "percentis"),
    _rule("outliers_area", "Dispersão global", "Após a primeira retirada, recalcula médias por posto usando km físico; calcula máximo por TA e P97 global desses máximos. TAs com máximo maior ou igual ao P97 saem do núcleo. Distribuições vazias não se aplicam.", "percentis"),
    _rule("allocation", "Alocação final com núcleo congelado", "Busca padrão a distância física estritamente menor que 500 km, com desempate por maior valor autorizado e código. Compara distância ao padrão e ao centro da TA mais próxima: menor ou igual prefere TA do padrão. Referência permite cruzar UF/segmento e usa média lat/lon dos centros congelados.", "alocacao"),
    _rule("no_core", "Ausência de núcleo e conservação", "Sem padrão elegível, cria área individual. Se todo núcleo for removido, todos recebem áreas individuais e motivo explícito. Nenhum posto espacialmente aceito desaparece; coordenadas ausentes/em revisão são listadas nas exclusões.", "alocacao"),
    _rule("exploratory", "Modo exploratório preservado", "Preserva agrupamento e decisões do motor exploratório, suas fronteiras UF/segmento, centro esférico e janela em dias. Partições sem eventos mantêm Ward geográfico e CR indisponível.", "modos"),
]


def method_description():
    return deepcopy({"reference_defaults": REFERENCE_DEFAULTS, "exploratory_defaults": EXPLORATORY_DEFAULTS, "rules": RULES, "corrections": CORRECTIONS, "limits": LIMITS, "notice": NOTICE + " Referência documentada com correções declaradas; não se promete equivalência bit a bit ao sistema legado."})
