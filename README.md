# Separador de Listas + Provas + Termos

App Streamlit para receber vários ZIPs de provas, listas e termos opcionais; cruzar por CPF; e gerar um único PDF gigante na ordem:

**LISTAS → PROVAS → TERMO(S), quando houver**

Também gera `TERMOS_NAO_ENCONTRADOS.pdf` com os termos sem CPF ou cujo CPF não foi encontrado nas listas/provas.

## Estrutura

```text
separador-listas-provas/
├── app.py
├── requirements.txt
└── .streamlit/
    └── config.toml
```
