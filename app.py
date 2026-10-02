
import io
import re
import zipfile
import tempfile
from pathlib import Path
from collections import defaultdict

import streamlit as st
from pypdf import PdfReader, PdfWriter

st.set_page_config(
    page_title="Separador de Listas + Provas + Termos",
    page_icon="📚",
    layout="wide",
)

# ------------------------------------------------------------
# REGRAS DE IDENTIFICAÇÃO
# ------------------------------------------------------------

# CPF com ou sem pontuação.
CPF_RE = re.compile(
    r"(?<!\d)(\d{3}\.?\d{3}\.?\d{3}-?\d{2})(?!\d)"
)

# Os códigos dos treinamentos seguem o padrão 40xxx.
# Ex.: 40794, 40795, ... 40803, 40814.
THEME_RE = re.compile(r"(?<!\d)(40\d{3})(?!\d)")


def normalize_cpf(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits if len(digits) == 11 else ""


def format_cpf(cpf: str) -> str:
    cpf = normalize_cpf(cpf)
    if len(cpf) != 11:
        return cpf
    return f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}"


def extract_cpfs(text: str):
    found = []
    for match in CPF_RE.findall(text or ""):
        cpf = normalize_cpf(match)
        if cpf and cpf not in found:
            found.append(cpf)
    return found


def extract_themes(text: str):
    found = []
    for match in THEME_RE.findall(text or ""):
        if match not in found:
            found.append(match)
    return found


def extract_theme(text: str, filename: str = "") -> str:
    # Primeiro tenta o nome do arquivo, pois as provas normalmente
    # possuem o código do treinamento no nome.
    for source in (filename or "", text or ""):
        themes = extract_themes(source)
        if themes:
            return themes[0]
    return ""


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip()


def name_from_filename(filename: str) -> str:
    """
    Esperado:
    CPF - 40794 - NOME - TEMA.pdf
    """
    stem = Path(filename).stem
    parts = [p.strip(" -_") for p in stem.split(" - ")]

    if len(parts) >= 3:
        # O terceiro bloco normalmente é o nome.
        return clean_name(parts[2])

    return ""


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:150] or "SEM_NOME"


def safe_extract_zip(uploaded_file, destination: Path):
    """
    Extrai ZIP evitando caminhos que tentem sair da pasta de destino.
    """
    destination = destination.resolve()

    with zipfile.ZipFile(uploaded_file) as z:
        for member in z.infolist():
            target = (destination / member.filename).resolve()

            if target != destination and destination not in target.parents:
                raise ValueError(
                    f"ZIP inválido: caminho inseguro encontrado ({member.filename})"
                )

            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(member) as source, open(target, "wb") as out:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)


def iter_pdf_files(folder: Path):
    if not folder.exists():
        return []

    return sorted(
        [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"],
        key=lambda p: str(p).lower(),
    )


def copy_pdf_pages(writer: PdfWriter, reader: PdfReader, page_indexes):
    for idx in page_indexes:
        if 0 <= idx < len(reader.pages):
            writer.add_page(reader.pages[idx])


# ------------------------------------------------------------
# LEITURA / INDEXAÇÃO
# ------------------------------------------------------------

@st.cache_data(show_spinner=False)
def scan_list_pdf(path_str: str, mtime_ns: int):
    """
    Lê uma lista de presença.
    Um PDF pode ter uma ou várias páginas.
    Cada página é associada ao CPF encontrado naquela página.
    """
    path = Path(path_str)
    reader = PdfReader(str(path))

    theme = extract_theme("", path.name)
    pages = []

    for page_number, page in enumerate(reader.pages):
        text = page.extract_text() or ""

        if not theme:
            theme = extract_theme(text, path.name)

        cpfs = extract_cpfs(text)

        # Em uma lista normal, a página pertence ao primeiro CPF
        # identificado naquela página.
        cpf = cpfs[0] if cpfs else ""

        # Tenta identificar o nome junto ao CPF.
        name = ""
        if cpf:
            cpf_formats = [
                re.escape(format_cpf(cpf)),
                re.escape(cpf),
            ]

            for cpf_pattern in cpf_formats:
                match = re.search(
                    r"([A-ZÁÉÍÓÚÀÃÕÇ][A-ZÁÉÍÓÚÀÃÕÇ .'-]{4,})\s+"
                    + cpf_pattern,
                    text,
                    re.IGNORECASE,
                )
                if match:
                    name = clean_name(match.group(1))
                    break

        pages.append(
            {
                "page": page_number,
                "cpf": cpf,
                "name": name,
            }
        )

    return {
        "theme": theme,
        "pages": pages,
        "total_pages": len(reader.pages),
    }


@st.cache_data(show_spinner=False)
def scan_proof_pdf(path_str: str, mtime_ns: int):
    path = Path(path_str)
    reader = PdfReader(str(path))

    full_text = "\n".join(
        (page.extract_text() or "") for page in reader.pages
    )

    # Primeiro procura no nome do arquivo.
    cpfs = extract_cpfs(path.name)
    if not cpfs:
        cpfs = extract_cpfs(full_text)

    cpf = cpfs[0] if cpfs else ""

    theme = extract_theme("", path.name)
    if not theme:
        theme = extract_theme(full_text, path.name)

    name = name_from_filename(path.name)

    if not name:
        match = re.search(
            r"Nome\s*:\s*([^\n]+)",
            full_text,
            re.IGNORECASE,
        )
        if match:
            name = clean_name(match.group(1))

    return {
        "cpf": cpf,
        "theme": theme,
        "name": name,
        "pages": len(reader.pages),
    }


@st.cache_data(show_spinner=False)
def scan_term_pdf(path_str: str, mtime_ns: int):
    """Lê termos de alteração, normalmente um termo por página."""
    path = Path(path_str)
    reader = PdfReader(str(path))
    pages = []

    for page_number, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        cpfs = extract_cpfs(text)
        cpf = cpfs[0] if cpfs else ""

        name = ""
        match = re.search(r"Eu,\s*(.+?),\s*CPF", text, re.IGNORECASE | re.DOTALL)
        if match:
            name = clean_name(match.group(1))

        date = ""
        date_match = re.search(
            r"Curitiba,\s*(\d{1,2}\s+de\s+\w+\s+de\s+\d{4})",
            text,
            re.IGNORECASE,
        )
        if date_match:
            date = date_match.group(1)

        pages.append(
            {
                "path": str(path),
                "page": page_number,
                "cpf": cpf,
                "name": name,
                "date": date,
            }
        )

    return pages


def build_index(list_files, proof_files, term_files=None, progress_callback=None):
    term_files = term_files or []

    lists_by_cpf = defaultdict(list)
    proofs_by_cpf = defaultdict(list)
    terms_by_cpf = defaultdict(list)
    names = {}

    list_rows = []
    proof_rows = []
    term_rows = []
    unmatched_terms = []

    total = max(len(list_files) + len(proof_files) + len(term_files), 1)
    done = 0

    # LISTAS
    for path in list_files:
        try:
            info = scan_list_pdf(str(path), path.stat().st_mtime_ns)
            theme = info["theme"]

            for item in info["pages"]:
                cpf = item["cpf"]
                if cpf:
                    lists_by_cpf[cpf].append(
                        {
                            "path": str(path),
                            "page": item["page"],
                            "theme": theme,
                        }
                    )
                    if item["name"]:
                        names.setdefault(cpf, item["name"])

            list_rows.append(
                {
                    "Arquivo": path.name,
                    "Tema": theme or "NÃO IDENTIFICADO",
                    "Páginas": info["total_pages"],
                }
            )
        except Exception as error:
            list_rows.append(
                {
                    "Arquivo": path.name,
                    "Tema": f"ERRO: {error}",
                    "Páginas": 0,
                }
            )

        done += 1
        if progress_callback:
            progress_callback(done / total)

    # PROVAS
    for path in proof_files:
        try:
            info = scan_proof_pdf(str(path), path.stat().st_mtime_ns)
            cpf = info["cpf"]

            if cpf:
                proofs_by_cpf[cpf].append(
                    {
                        "path": str(path),
                        "theme": info["theme"],
                    }
                )
                if info["name"]:
                    names.setdefault(cpf, info["name"])

            proof_rows.append(
                {
                    "Arquivo": path.name,
                    "CPF": format_cpf(cpf) if cpf else "NÃO IDENTIFICADO",
                    "Tema": info["theme"] or "NÃO IDENTIFICADO",
                }
            )
        except Exception as error:
            proof_rows.append(
                {
                    "Arquivo": path.name,
                    "CPF": f"ERRO: {error}",
                    "Tema": "",
                }
            )

        done += 1
        if progress_callback:
            progress_callback(done / total)

    # TERMOS (OPCIONAL)
    for path in term_files:
        try:
            pages = scan_term_pdf(str(path), path.stat().st_mtime_ns)
            for item in pages:
                cpf = item["cpf"]
                term_rows.append(
                    {
                        "Arquivo": path.name,
                        "Página": item["page"] + 1,
                        "CPF": format_cpf(cpf) if cpf else "NÃO IDENTIFICADO",
                        "Nome": item["name"],
                        "Data": item["date"],
                    }
                )

                if cpf:
                    terms_by_cpf[cpf].append(item)
                    if item["name"]:
                        names.setdefault(cpf, item["name"])
                else:
                    # Sem CPF: não dá para vincular automaticamente.
                    unmatched_terms.append(
                        {
                            **item,
                            "reason": "CPF NÃO IDENTIFICADO",
                        }
                    )
        except Exception as error:
            term_rows.append(
                {
                    "Arquivo": path.name,
                    "Página": "",
                    "CPF": f"ERRO: {error}",
                    "Nome": "",
                    "Data": "",
                }
            )

        done += 1
        if progress_callback:
            progress_callback(done / total)

    # Qualquer termo com CPF que não aparece nas listas/provas vai para revisão manual.
    people_cpfs = set(lists_by_cpf) | set(proofs_by_cpf)
    for cpf, items in terms_by_cpf.items():
        if cpf not in people_cpfs:
            for item in items:
                unmatched_terms.append(
                    {
                        **item,
                        "reason": "CPF NÃO ENCONTRADO NAS LISTAS/PROVAS",
                    }
                )

    return (
        lists_by_cpf,
        proofs_by_cpf,
        terms_by_cpf,
        names,
        list_rows,
        proof_rows,
        term_rows,
        unmatched_terms,
    )


def theme_sort_key(entry):
    theme = entry.get("theme", "")

    if theme.isdigit():
        return (0, int(theme), entry.get("path", ""), entry.get("page", -1))

    return (1, 999999, entry.get("path", ""), entry.get("page", -1))


# ------------------------------------------------------------
# GERAÇÃO DO PDF ÚNICO
# ------------------------------------------------------------

def generate_big_pdf(
    cpfs,
    lists_by_cpf,
    proofs_by_cpf,
    terms_by_cpf,
    names,
    output_path,
    progress_callback=None,
):
    """
    Gera UM ÚNICO PDF gigante:
      colaborador -> listas -> provas -> termo(s), se houver
    """
    writer = PdfWriter()
    total = max(len(cpfs), 1)
    current_page = 0

    def term_sort_key(entry):
        return (
            entry.get("date", ""),
            entry.get("path", ""),
            entry.get("page", -1),
        )

    for position, cpf in enumerate(cpfs, start=1):
        name = names.get(cpf, "SEM NOME")
        first_page_of_person = current_page

        # 1. LISTAS
        for entry in sorted(lists_by_cpf.get(cpf, []), key=theme_sort_key):
            reader = PdfReader(entry["path"])
            copy_pdf_pages(writer, reader, [entry["page"]])
            current_page += 1

        # 2. PROVAS
        for entry in sorted(proofs_by_cpf.get(cpf, []), key=theme_sort_key):
            reader = PdfReader(entry["path"])
            copy_pdf_pages(writer, reader, range(len(reader.pages)))
            current_page += len(reader.pages)

        # 3. TERMO(S) OPCIONAL(IS) — sempre depois das provas
        for entry in sorted(terms_by_cpf.get(cpf, []), key=term_sort_key):
            reader = PdfReader(entry["path"])
            copy_pdf_pages(writer, reader, [entry["page"]])
            current_page += 1

        if current_page > first_page_of_person:
            try:
                writer.add_outline_item(
                    f"{name} - {format_cpf(cpf)}",
                    first_page_of_person,
                )
            except Exception:
                pass

        if progress_callback:
            progress_callback(position / total)

    with open(output_path, "wb") as output:
        writer.write(output)


def generate_unmatched_terms_pdf(unmatched_terms, output_path):
    """Gera um PDF separado só com os termos que precisam de revisão manual."""
    writer = PdfWriter()

    def key(item):
        return (item.get("path", ""), item.get("page", -1))

    for item in sorted(unmatched_terms, key=key):
        reader = PdfReader(item["path"])
        copy_pdf_pages(writer, reader, [item["page"]])

    with open(output_path, "wb") as output:
        writer.write(output)


# ------------------------------------------------------------
# INTERFACE
# ------------------------------------------------------------

def main():
    st.title("📚 Separador de Listas + Provas + Termos")
    st.markdown(
        """
        **Regra do sistema:** o CPF é a chave principal.

        Para cada colaborador, o PDF final ficará assim:

        **TODAS AS LISTAS → TODAS AS PROVAS → TERMO(S), quando houver**

        As listas e provas são organizadas pela ordem numérica do código
        do treinamento (ex.: 40794, 40795, ... 40814).

        Os termos de alteração são opcionais: se não existir termo para um CPF,
        o colaborador entra normalmente, sem termo.
        """
    )

    st.info(
        "💡 Para milhares de PDFs, compacte cada grupo em ZIP. "
        "O aplicativo aceita um ou vários ZIPs em cada campo."
    )

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("📦 PROVAS")
        proof_zips = st.file_uploader(
            "Envie o(s) ZIP(s) das provas",
            type=["zip"],
            accept_multiple_files=True,
            key="proofs",
            help="Você pode enviar um ZIP grande ou vários ZIPs.",
        )

    with col2:
        st.subheader("📦 LISTAS POR TEMA")
        list_zips = st.file_uploader(
            "Envie o(s) ZIP(s) das listas",
            type=["zip"],
            accept_multiple_files=True,
            key="lists",
            help="Você pode enviar um ZIP grande ou vários ZIPs.",
        )

    st.subheader("🩺 TERMOS DE ALTERAÇÃO DE EXAMES/ASOS — opcional")
    term_zips = st.file_uploader(
        "Envie o(s) ZIP(s) dos termos. Pode deixar vazio quando a turma não tiver termos.",
        type=["zip"],
        accept_multiple_files=True,
        key="terms",
        help="Um termo por página é aceito. O CPF é usado para fazer o vínculo automático.",
    )

    if not proof_zips or not list_zips:
        st.warning(
            "Envie pelo menos um ZIP de PROVAS e um ZIP de LISTAS para continuar."
        )
        st.stop()

    # --------------------------------------------------------
    # EXTRAÇÃO
    # --------------------------------------------------------

    if st.button(
        "🔎 ANALISAR ARQUIVOS",
        type="primary",
        use_container_width=True,
    ):
        with st.spinner("Extraindo os ZIPs..."):
            temp_root = Path(
                tempfile.mkdtemp(
                    prefix="separador_listas_provas_"
                )
            )

            proof_root = temp_root / "provas"
            list_root = temp_root / "listas"
            term_root = temp_root / "termos"

            proof_root.mkdir(parents=True, exist_ok=True)
            list_root.mkdir(parents=True, exist_ok=True)
            term_root.mkdir(parents=True, exist_ok=True)

            try:
                for index, uploaded_zip in enumerate(proof_zips, start=1):
                    safe_extract_zip(
                        uploaded_zip,
                        proof_root / f"zip_{index}",
                    )

                for index, uploaded_zip in enumerate(list_zips, start=1):
                    safe_extract_zip(
                        uploaded_zip,
                        list_root / f"zip_{index}",
                    )

                for index, uploaded_zip in enumerate(term_zips, start=1):
                    safe_extract_zip(
                        uploaded_zip,
                        term_root / f"zip_{index}",
                    )

            except Exception as error:
                st.error(f"Erro ao extrair ZIP: {error}")
                st.stop()

            proof_files = iter_pdf_files(proof_root)
            list_files = iter_pdf_files(list_root)
            term_files = iter_pdf_files(term_root)

            st.session_state["temp_root"] = str(temp_root)
            st.session_state["proof_files"] = [
                str(p) for p in proof_files
            ]
            st.session_state["list_files"] = [
                str(p) for p in list_files
            ]
            st.session_state["term_files"] = [
                str(p) for p in term_files
            ]

    if "proof_files" not in st.session_state:
        st.stop()

    proof_files = [
        Path(p) for p in st.session_state["proof_files"]
    ]

    list_files = [
        Path(p) for p in st.session_state["list_files"]
    ]
    term_files = [
        Path(p) for p in st.session_state.get("term_files", [])
    ]

    st.success(
        f"Encontrados **{len(proof_files):,} PDFs de provas**, "
        f"**{len(list_files):,} PDFs de listas** e "
        f"**{len(term_files):,} PDFs de termos**."
    )

    if not proof_files:
        st.error("Nenhuma prova em PDF foi encontrada.")
        st.stop()

    if not list_files:
        st.error("Nenhuma lista em PDF foi encontrada.")
        st.stop()

    # --------------------------------------------------------
    # INDEXAÇÃO
    # --------------------------------------------------------

    if "index" not in st.session_state:
        st.subheader("🔍 Cruzamento por CPF")

        index_progress = st.progress(0)

        with st.spinner("Lendo os PDFs e identificando CPFs..."):
            index = build_index(
                list_files,
                proof_files,
                term_files,
                progress_callback=lambda value: index_progress.progress(
                    int(value * 100)
                ),
            )

        index_progress.progress(100)
        st.session_state["index"] = index

    (
        lists_by_cpf,
        proofs_by_cpf,
        terms_by_cpf,
        names,
        list_rows,
        proof_rows,
        term_rows,
        unmatched_terms,
    ) = st.session_state["index"]

    # --------------------------------------------------------
    # TABELA DE CONFERÊNCIA
    # --------------------------------------------------------

    cpfs = sorted(
        set(lists_by_cpf) | set(proofs_by_cpf),
        key=lambda cpf: (
            names.get(cpf, "").upper(),
            cpf,
        ),
    )

    rows = []

    for cpf in cpfs:
        list_count = len(lists_by_cpf.get(cpf, []))
        proof_count = len(proofs_by_cpf.get(cpf, []))
        term_count = len(terms_by_cpf.get(cpf, []))

        if list_count and proof_count:
            status = "OK"
        elif list_count:
            status = "SEM PROVA"
        else:
            status = "SEM LISTA"

        rows.append(
            {
                "CPF": format_cpf(cpf),
                "Nome": names.get(cpf, ""),
                "Listas": list_count,
                "Provas": proof_count,
                "Termos": term_count,
                "Situação": status,
            }
        )

    st.subheader("📋 Conferência")

    st.dataframe(
        rows,
        use_container_width=True,
        hide_index=True,
    )

    complete = [
        r for r in rows
        if r["Situação"] == "OK"
    ]

    no_list = [
        r for r in rows
        if r["Situação"] == "SEM LISTA"
    ]

    no_proof = [
        r for r in rows
        if r["Situação"] == "SEM PROVA"
    ]

    term_person_count = sum(1 for cpf in cpfs if terms_by_cpf.get(cpf))

    m1, m2, m3, m4 = st.columns(4)

    m1.metric("Colaboradores", len(rows))
    m2.metric("Completos", len(complete))
    m3.metric("Com termo", term_person_count)
    m4.metric("Termos para revisão", len(unmatched_terms))

    with st.expander("📄 Detalhes das listas"):
        st.dataframe(
            list_rows,
            use_container_width=True,
            hide_index=True,
        )

    with st.expander("📝 Detalhes das provas"):
        st.dataframe(
            proof_rows,
            use_container_width=True,
            hide_index=True,
        )

    if term_files:
        with st.expander("🩺 Detalhes dos termos"):
            st.dataframe(
                term_rows,
                use_container_width=True,
                hide_index=True,
            )

        if unmatched_terms:
            st.warning(
                f"{len(unmatched_terms)} página(s) de termo não foram vinculadas "
                "automaticamente. Elas serão colocadas em um PDF separado "
                "para você fazer a separação manual."
            )

    # --------------------------------------------------------
    # GERAÇÃO
    # --------------------------------------------------------

    st.divider()
    st.subheader("🖨️ Gerar PDF único para impressão")

    st.write(
        """
        O sistema **não cria um PDF separado para cada pessoa**.

        Ele cria **um único PDF gigante**, seguindo esta estrutura:

        `Pessoa 1 → Listas → Provas → Termo (se houver) → Pessoa 2 → ...`
        """
    )

    only_complete = st.checkbox(
        "Gerar somente colaboradores com LISTA + PROVA",
        value=True,
    )

    selected_labels = st.multiselect(
        "Opcional: escolha colaboradores específicos "
        "(deixe vazio para todos)",
        options=[
            f"{r['CPF']} — {r['Nome']}"
            for r in rows
        ],
    )

    selected_cpfs = None

    if selected_labels:
        selected_cpfs = {
            normalize_cpf(
                label.split(" — ", 1)[0]
            )
            for label in selected_labels
        }

    if st.button(
        "🚀 GERAR PDF GIGANTE",
        type="primary",
        use_container_width=True,
    ):
        if selected_cpfs:
            target_cpfs = [
                cpf for cpf in cpfs
                if cpf in selected_cpfs
            ]
        else:
            target_cpfs = cpfs.copy()

        if only_complete:
            target_cpfs = [
                cpf for cpf in target_cpfs
                if lists_by_cpf.get(cpf)
                and proofs_by_cpf.get(cpf)
            ]

        if not target_cpfs:
            st.error(
                "Nenhum colaborador atende aos filtros escolhidos."
            )
            st.stop()

        with tempfile.TemporaryDirectory(
            prefix="pdf_final_"
        ) as output_dir:

            output_path = (
                Path(output_dir)
                / "LISTAS_PROVAS_TERMOS_COMPLETO.pdf"
            )

            progress = st.progress(0)

            with st.spinner(
                "Montando o PDF único... isso pode levar alguns minutos."
            ):
                generate_big_pdf(
                    target_cpfs,
                    lists_by_cpf,
                    proofs_by_cpf,
                    terms_by_cpf,
                    names,
                    output_path,
                    progress_callback=lambda value: progress.progress(
                        int(value * 100)
                    ),
                )

            progress.progress(100)

            pdf_bytes = output_path.read_bytes()

            st.success(
                f"PDF gerado com sucesso! "
                f"{len(target_cpfs):,} colaboradores incluídos."
            )

            st.download_button(
                "⬇️ BAIXAR PDF GIGANTE",
                data=pdf_bytes,
                file_name="LISTAS_PROVAS_TERMOS_COMPLETO.pdf",
                mime="application/pdf",
                use_container_width=True,
            )

            if unmatched_terms:
                unmatched_path = Path(output_dir) / "TERMOS_NAO_ENCONTRADOS.pdf"
                generate_unmatched_terms_pdf(unmatched_terms, unmatched_path)
                unmatched_bytes = unmatched_path.read_bytes()

                st.warning(
                    "Também foi criado um segundo PDF contendo somente os "
                    "termos sem correspondência automática."
                )

                st.download_button(
                    "⬇️ BAIXAR TERMOS NÃO ENCONTRADOS",
                    data=unmatched_bytes,
                    file_name="TERMOS_NAO_ENCONTRADOS.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )

            st.caption(
                f"Tamanho do arquivo: "
                f"{len(pdf_bytes) / (1024 * 1024):.1f} MB"
            )

            st.info(
                "📑 O PDF possui marcadores internos por colaborador "
                "quando o leitor de PDF oferece suporte a marcadores."
            )

    st.divider()

    st.caption(
        "Chave de cruzamento: CPF. "
        "O nome é usado apenas para identificação. "
        "Termos são opcionais e entram somente quando o CPF é encontrado."
    )


if __name__ == "__main__":
    main()
