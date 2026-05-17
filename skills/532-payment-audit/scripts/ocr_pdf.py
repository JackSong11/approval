import os
import sys
import tempfile
import requests
import fitz  # PyMuPDF


# def _create_converter():
#     api_key = os.getenv("JOY_BUILDER_API_KEY")
#     api_base = os.getenv("JOY_BUILDER_BASE_URL")
#
#     if not api_key or not api_base:
#         # 错误信息重定向到 stderr，不影响 stdout 的结果返回
#         sys.stderr.write("Error: Environmental variables JOY_BUILDER_API_KEY/BASE_URL not set.\n")
#         sys.exit(1)
#
#     vlm_options = ApiVlmOptions(
#         url=f"{api_base}/chat/completions",
#         params=dict(model="DeepSeek-OCR", temperature=0.0),
#         headers={"Authorization": f"Bearer {api_key}"},
#         prompt="<image>\nPlease perform OCR on this image and output the extracted text in Markdown format.",
#         timeout=600,
#         scale=2.0,
#         response_format=ResponseFormat.MARKDOWN,
#     )
#
#     return DocumentConverter(
#         format_options={
#             InputFormat.PDF: PdfFormatOption(
#                 pipeline_options=VlmPipelineOptions(enable_remote_services=True, vlm_options=vlm_options),
#                 pipeline_cls=VlmPipeline,
#             )
#         }
#     )


def identify_pdf_type(file_path, threshold=10):
    """判断是扫描件还是电子档"""
    try:
        doc = fitz.open(file_path)
        check_pages = min(len(doc), 5)
        scanned_count = sum(1 for i in range(check_pages) if len(doc[i].get_text().strip()) < threshold)
        doc.close()
        return "scanned" if scanned_count > (check_pages / 2) else "digital"
    except:
        return "scanned"


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: python ocr_pdf.py [PDF_PATH_OR_URL]\n")
        sys.exit(1)

    input_source = sys.argv[1]
    tmp_path = None

    try:
        # 1. 处理下载
        if input_source.startswith(('http://', 'https://')):
            resp = requests.get(input_source, timeout=300)
            resp.raise_for_status()
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                tmp.write(resp.content)
                tmp_path = tmp.name
            local_path = tmp_path
        else:
            local_path = input_source

        # 2. 逻辑分发
        result_text = ""
        if identify_pdf_type(local_path) == "digital":
            doc = fitz.open(local_path)
            # 电子档直接拼凑文本输出
            result_text = "\n\n".join([page.get_text().strip() for page in doc])
            doc.close()
        # else:
            # 扫描件调用 OCR
            # converter = _create_converter()
            # conv_res = converter.convert(input_source)
            # result_text = conv_res.document.export_to_markdown()

        # 3. 最终通过 stdout 返回，bash 可以直接捕获
        sys.stdout.write(result_text)

    except Exception as e:
        sys.stderr.write(f"Execution Error: {str(e)}\n")
        sys.exit(1)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


if __name__ == "__main__":
    main()