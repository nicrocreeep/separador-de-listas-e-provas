import re
import shutil
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path

import streamlit as st
from pypdf import PdfReader

try:
    import pikepdf
except ImportError:
    pikepdf = None


st.set_page_config(
    page_title="Separador de Listas + Provas + Termos",
    page_icon="📚",
    layout="wide",
)

CPF_RE = re.compile(r"(?<!\d)(\d{3}\.?\d{3}\.?\d{3}-?\d{2})(?!\d)")
THEME_RE = re.compile(r"(?<!\d)(40\d{3})(?!\d)")


def normalize_cpf(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits if len(digits) == 11 else ""


def format_cpf(cpf: str) -> str:
    cpf = normalize_cpf(cpf)
    if len(cpf) != 11:
        return cpf
    return f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}"


def extract_cpfs(text: str) -> list[str]:
    found = []
    for match in CPF_RE.findall(text or ""):
        cpf = normalize_cpf(match)
        if cpf and cpf not in found:
            found.append(cpf)
    return found


def extract_theme(text: str = "", filename: str = "") -> str:
    for source in (filename or "", text or ""):
        match = THEME_RE.search(source)
        if match:
            return match.group(1)
    return ""


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip()


def name_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    parts = [p.strip(" -_") for p in stem.split(" - ")]
    if len(parts) >= 3:
        return clean_name(parts[2])
    return ""


def safe_extract_zip(uploaded_file, destination: Path) -> None:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(uploaded_file) as zf:
        for member in zf.infolist():
            target = (destination / member.filename).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError(
                    f"ZIP inválido: caminho inseguro encontrado: {member.filename}"
                )
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)


def iter_pdf_files(folder: Path) -> list[Path]:
    return sorted(
        [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"],
        key=lambda p: str(p).lower(),
    )


# ============================================================
# INDEXAÇÃO
# ============================================================

@st.cache_data(show_spinner=False)
def scan_list_pdf(path_str: str, mtime_ns: int) -> dict:
    path = Path(path_str)
    reader = PdfReader(str(path))
    theme = extract_theme(filename=path.name)
    pages = []

    for page_number, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if not theme:
            theme = extract_theme(text=text, filename=path.name)

        cpfs = extract_cpfs(text)
        cpf = cpfs[0] if cpfs else ""
        name = ""

        if cpf:
            escaped = re.escape(format_cpf(cpf))
            match = re.search(
                rf"([A-ZÁÉÍÓÚÀÃÕÇ][A-ZÁÉÍÓÚÀÃÕÇ .'-]{{4,}})\s+{escaped}",
                text,
                re.IGNORECASE,
            )
            if match:
                name = clean_name(match.group(1))

        pages.append({"page": page_number, "cpf": cpf, "name": name})

    return {"theme": theme, "pages": pages, "total_pages": len(reader.pages)}


@st.cache_data(show_spinner=False)
def scan_proof_metadata(path_str: str, mtime_ns: int) -> dict:
    """
    Otimização importante:
    a maioria das provas já tem CPF/código/nome no nome do arquivo.
    Nesse caso, não precisamos abrir o PDF nesta etapa.
    """
    path = Path(path_str)

    cpf_candidates = extract_cpfs(path.name)
    theme = extract_theme(filename=path.name)
    name = name_from_filename(path.name)

    if cpf_candidates and theme and name:
        return {
            "cpf": cpf_candidates[0],
            "theme": theme,
            "name": name,
            "pages": 0,
            "needs_pdf_read": True,
        }

    # Fallback somente para arquivos fora do padrão.
    reader = PdfReader(str(path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)

    if not cpf_candidates:
        cpf_candidates = extract_cpfs(text)

    if not theme:
        theme = extract_theme(text=text, filename=path.name)

    if not name:
        match = re.search(r"Nome\s*:\s*([^\n]+)", text, re.IGNORECASE)
        if match:
            name = clean_name(match.group(1))

    return {
        "cpf": cpf_candidates[0] if cpf_candidates else "",
        "theme": theme,
        "name": name,
        "pages": len(reader.pages),
        "needs_pdf_read": True,
    }


@st.cache_data(show_spinner=False)
def get_pdf_page_count(path_str: str, mtime_ns: int) -> int:
    reader = PdfReader(str(path_str))
    return len(reader.pages)


@st.cache_data(show_spinner=False)
def scan_term_pdf(path_str: str, mtime_ns: int) -> list[dict]:
    path = Path(path_str)
    reader = PdfReader(str(path))
    pages = []

    for page_number, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        cpfs = extract_cpfs(text)
        cpf = cpfs[0] if cpfs else ""

        name = ""
        match = re.search(
            r"Eu,\s*(.+?),\s*CPF",
            text,
            re.IGNORECASE | re.DOTALL,
        )
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

    # LISTAS: leitura página a página é necessária.
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
                {"Arquivo": path.name, "Tema": f"ERRO: {error}", "Páginas": 0}
            )

        done += 1
        if progress_callback:
            progress_callback(done / total)

    # PROVAS: normalmente só lê nome do arquivo.
    for path in proof_files:
        try:
            info = scan_proof_metadata(str(path), path.stat().st_mtime_ns)
            cpf = info["cpf"]

            if cpf:
                proofs_by_cpf[cpf].append(
                    {"path": str(path), "theme": info["theme"]}
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
                {"Arquivo": path.name, "CPF": f"ERRO: {error}", "Tema": ""}
            )

        done += 1
        if progress_callback:
            progress_callback(done / total)

    # TERMOS: um PDF pode conter vários termos, um por página.
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
                    unmatched_terms.append({**item, "reason": "CPF NÃO IDENTIFICADO"})
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


def term_sort_key(entry):
    return (
        entry.get("date", ""),
        entry.get("path", ""),
        entry.get("page", -1),
    )


# ============================================================
# GERAÇÃO DO PDF — qpdf via pikepdf
# ============================================================

def require_pikepdf():
    if pikepdf is None:
        raise RuntimeError(
            "pikepdf não está instalado. Atualize o requirements.txt e faça um novo deploy."
        )


def generate_big_pdf(
    cpfs,
    lists_by_cpf,
    proofs_by_cpf,
    terms_by_cpf,
    names,
    output_path: Path,
    progress_callback=None,
):
    """
    Gera o PDF diretamente a partir dos arquivos em disco usando qpdf/pikepdf.

    Isso evita manter milhares de páginas dentro de um PdfWriter em memória.
    """
    require_pikepdf()

    job = pikepdf.JobBuilder().empty().output(str(output_path))
    total = max(len(cpfs), 1)

    for position, cpf in enumerate(cpfs, start=1):
        # LISTAS
        for entry in sorted(lists_by_cpf.get(cpf, []), key=theme_sort_key):
            page_number = entry["page"] + 1  # qpdf usa página 1-based.
            job = job.add_pages(entry["path"], str(page_number))

        # PROVAS
        for entry in sorted(proofs_by_cpf.get(cpf, []), key=theme_sort_key):
            job = job.add_pages(entry["path"])

        # TERMOS
        for entry in sorted(terms_by_cpf.get(cpf, []), key=term_sort_key):
            page_number = entry["page"] + 1
            job = job.add_pages(entry["path"], str(page_number))

        if progress_callback:
            progress_callback(position / total)

    job.run()


def generate_unmatched_terms_pdf(unmatched_terms, output_path: Path):
    require_pikepdf()

    job = pikepdf.JobBuilder().empty().output(str(output_path))
    for item in sorted(
        unmatched_terms,
        key=lambda x: (x.get("path", ""), x.get("page", -1)),
    ):
        page_number = item["page"] + 1
        job = job.add_pages(item["path"], str(page_number))
    job.run()


# ============================================================
# DOWNLOAD LAZY — não carrega o PDF gigante na RAM antes do clique
# ============================================================

def download_file(path: str):
    return open(path, "rb")


# ============================================================
# INTERFACE
# ============================================================

def main():
    st.title("📚 Separador de Listas + Provas + Termos")
    st.markdown(
        """
        **Chave principal: CPF.**

        Para cada colaborador:

        **LISTAS → PROVAS → TERMO(S), quando houver**

        O resultado principal é **um único PDF gigante**, pronto para impressão.
        """
    )

    st.info(
        "💡 Pode enviar vários ZIPs em cada campo. Ex.: turma_13.zip + turma_121.zip."
    )

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("📦 PROVAS")
        proof_zips = st.file_uploader(
            "Envie o(s) ZIP(s) das provas",
            type=["zip"],
            accept_multiple_files=True,
            key="proofs",
            help="Um ZIP por turma ou quantos forem necessários.",
        )

    with col2:
        st.subheader("📦 LISTAS POR TEMA")
        list_zips = st.file_uploader(
            "Envie o(s) ZIP(s) das listas",
            type=["zip"],
            accept_multiple_files=True,
            key="lists",
            help="Um ZIP por turma ou quantos forem necessários.",
        )

    st.subheader("🩺 TERMOS DE ALTERAÇÃO DE EXAMES/ASOS — opcional")
    term_zips = st.file_uploader(
        "Envie o(s) ZIP(s) dos termos. Um PDF pode conter vários termos, um por página.",
        type=["zip"],
        accept_multiple_files=True,
        key="terms",
    )

    if not proof_zips or not list_zips:
        st.warning("Envie pelo menos um ZIP de PROVAS e um ZIP de LISTAS.")
        st.stop()

    if st.button("🔎 ANALISAR ARQUIVOS", type="primary", width="stretch"):
        # Descarta análise e arquivos anteriores para liberar espaço.
        old_root = st.session_state.get("temp_root")
        if old_root:
            shutil.rmtree(old_root, ignore_errors=True)

        for key in (
            "temp_root",
            "proof_files",
            "list_files",
            "term_files",
            "index",
            "final_pdf",
            "unmatched_pdf",
        ):
            st.session_state.pop(key, None)

        temp_root = Path(tempfile.mkdtemp(prefix="separador_listas_provas_"))
        proof_root = temp_root / "provas"
        list_root = temp_root / "listas"
        term_root = temp_root / "termos"
        proof_root.mkdir(parents=True)
        list_root.mkdir(parents=True)
        term_root.mkdir(parents=True)

        with st.spinner("Extraindo os ZIPs..."):
            try:
                for index, uploaded_zip in enumerate(proof_zips, start=1):
                    safe_extract_zip(uploaded_zip, proof_root / f"zip_{index}")
                for index, uploaded_zip in enumerate(list_zips, start=1):
                    safe_extract_zip(uploaded_zip, list_root / f"zip_{index}")
                for index, uploaded_zip in enumerate(term_zips, start=1):
                    safe_extract_zip(uploaded_zip, term_root / f"zip_{index}")
            except Exception as error:
                shutil.rmtree(temp_root, ignore_errors=True)
                st.error(f"Erro ao extrair ZIP: {error}")
                st.stop()

        st.session_state["temp_root"] = str(temp_root)
        st.session_state["proof_files"] = [str(p) for p in iter_pdf_files(proof_root)]
        st.session_state["list_files"] = [str(p) for p in iter_pdf_files(list_root)]
        st.session_state["term_files"] = [str(p) for p in iter_pdf_files(term_root)]

    if "proof_files" not in st.session_state:
        st.stop()

    proof_files = [Path(p) for p in st.session_state["proof_files"]]
    list_files = [Path(p) for p in st.session_state["list_files"]]
    term_files = [Path(p) for p in st.session_state.get("term_files", [])]

    c1, c2, c3 = st.columns(3)
    c1.metric("PDFs de provas", len(proof_files))
    c2.metric("PDFs de listas", len(list_files))
    c3.metric("PDFs de termos", len(term_files))

    if not proof_files:
        st.error("Nenhum PDF de prova foi encontrado.")
        st.stop()
    if not list_files:
        st.error("Nenhum PDF de lista foi encontrado.")
        st.stop()

    if "index" not in st.session_state:
        st.subheader("🔍 Cruzamento por CPF")
        progress = st.progress(0)
        with st.spinner("Analisando documentos..."):
            index = build_index(
                list_files,
                proof_files,
                term_files,
                progress_callback=lambda value: progress.progress(int(value * 100)),
            )
        progress.progress(100)
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

    cpfs = sorted(
        set(lists_by_cpf) | set(proofs_by_cpf),
        key=lambda cpf: (names.get(cpf, "").upper(), cpf),
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
    st.dataframe(rows, width="stretch", hide_index=True)

    complete = [r for r in rows if r["Situação"] == "OK"]
    term_person_count = sum(1 for cpf in cpfs if terms_by_cpf.get(cpf))

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Colaboradores", len(rows))
    m2.metric("Completos", len(complete))
    m3.metric("Com termo", term_person_count)
    m4.metric("Termos para revisão", len(unmatched_terms))

    with st.expander("📄 Detalhes das listas"):
        st.dataframe(list_rows, width="stretch", hide_index=True)

    with st.expander("📝 Detalhes das provas"):
        st.dataframe(proof_rows, width="stretch", hide_index=True)

    if term_files:
        with st.expander("🩺 Detalhes dos termos"):
            st.dataframe(term_rows, width="stretch", hide_index=True)

        if unmatched_terms:
            st.warning(
                f"{len(unmatched_terms)} página(s) de termo não foram vinculadas automaticamente. "
                "Elas serão colocadas em um PDF separado para revisão manual."
            )

    st.divider()
    st.subheader("🖨️ Gerar PDF único para impressão")
    st.write(
        "`Pessoa 1 → Listas → Provas → Termo (se houver) → Pessoa 2 → ...`"
    )

    only_complete = st.checkbox(
        "Gerar somente colaboradores com LISTA + PROVA",
        value=True,
    )

    selected_labels = st.multiselect(
        "Opcional: escolha colaboradores específicos (vazio = todos)",
        options=[f"{r['CPF']} — {r['Nome']}" for r in rows],
    )

    if st.button("🚀 GERAR PDF GIGANTE", type="primary", width="stretch"):
        if pikepdf is None:
            st.error(
                "A versão otimizada usa pikepdf. Atualize o requirements.txt e aguarde o novo deploy."
            )
            st.stop()

        selected_cpfs = None
        if selected_labels:
            selected_cpfs = {
                normalize_cpf(label.split(" — ", 1)[0])
                for label in selected_labels
            }

        target_cpfs = [
            cpf for cpf in cpfs
            if selected_cpfs is None or cpf in selected_cpfs
        ]

        if only_complete:
            target_cpfs = [
                cpf for cpf in target_cpfs
                if lists_by_cpf.get(cpf) and proofs_by_cpf.get(cpf)
            ]

        if not target_cpfs:
            st.error("Nenhum colaborador atende aos filtros escolhidos.")
            st.stop()

        # Mantém a pasta viva na sessão para o download lazy.
        root = Path(st.session_state["temp_root"])
        output_path = root / "LISTAS_PROVAS_TERMOS_COMPLETO.pdf"
        unmatched_path = root / "TERMOS_NAO_ENCONTRADOS.pdf"

        # Remove resultados anteriores.
        output_path.unlink(missing_ok=True)
        unmatched_path.unlink(missing_ok=True)

        progress = st.progress(0)

        with st.spinner(
            "Montando o PDF gigante com qpdf... isso pode levar alguns minutos."
        ):
            try:
                generate_big_pdf(
                    target_cpfs,
                    lists_by_cpf,
                    proofs_by_cpf,
                    terms_by_cpf,
                    names,
                    output_path,
                    progress_callback=lambda value: progress.progress(int(value * 100)),
                )
            except Exception as error:
                st.error(f"Erro durante a geração do PDF: {error}")
                st.stop()

        progress.progress(100)
        st.session_state["final_pdf"] = str(output_path)

        st.success(
            f"PDF gigante criado com sucesso: {len(target_cpfs):,} colaboradores."
        )
        st.caption(
            f"Tamanho: {output_path.stat().st_size / (1024 * 1024):.1f} MB"
        )

        st.download_button(
            "⬇️ BAIXAR PDF GIGANTE",
            data=lambda: download_file(str(output_path)),
            file_name="LISTAS_PROVAS_TERMOS_COMPLETO.pdf",
            mime="application/pdf",
            on_click="ignore",
            width="stretch",
        )

        if unmatched_terms:
            with st.spinner("Separando os termos que precisam de revisão manual..."):
                try:
                    generate_unmatched_terms_pdf(unmatched_terms, unmatched_path)
                except Exception as error:
                    st.warning(
                        f"O PDF principal foi gerado, mas o PDF de termos para revisão falhou: {error}"
                    )
                else:
                    st.session_state["unmatched_pdf"] = str(unmatched_path)
                    st.warning(
                        f"{len(unmatched_terms)} página(s) de termo precisam de revisão manual."
                    )
                    st.download_button(
                        "⬇️ BAIXAR TERMOS NÃO ENCONTRADOS",
                        data=lambda: download_file(str(unmatched_path)),
                        file_name="TERMOS_NAO_ENCONTRADOS.pdf",
                        mime="application/pdf",
                        on_click="ignore",
                        width="stretch",
                    )

    st.divider()
    st.caption(
        "CPF = chave principal. Termos são opcionais. Um PDF de termos pode conter vários termos, "
        "um por página; cada página é vinculada pelo CPF."
    )


if __name__ == "__main__":
    main()
