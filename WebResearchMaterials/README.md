# Ứng dụng quản lý đề tài và tài liệu nghiên cứu

Ứng dụng FastAPI nội bộ giúp quản lý đề tài, tìm tài liệu từ Scopus và Web of Science, tự động lấy PDF công khai, theo dõi trạng thái và giữ lịch sử. Chức năng Tra cứu bằng AI chỉ phân tích những PDF đã lưu thuộc các lần tìm kiếm do người dùng chọn.

## Cài đặt

Yêu cầu Python 3.11 trở lên, khuyến nghị Python 3.12.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Sao chép `.env.example` thành `.env` nếu chưa có rồi điền cấu hình PostgreSQL. Không đưa `.env` vào Git. Ứng dụng đọc trực tiếp các biến `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGSSLMODE`, `PGCONNECT_TIMEOUT` và `PGAPPNAME`.

Nên tạo `APP_SECRET_KEY` bằng chuỗi ngẫu nhiên dài. Đặt `COOKIE_SECURE=true` khi ứng dụng chạy qua HTTPS. `UPLOAD_DIR` mặc định là thư mục `uploads` trong dự án; tệp được lưu bằng tên UUID, còn tên gốc chỉ lưu trong database.

Với database mới, khởi tạo một lần bằng `schema.sql`. Với database của phiên bản trước, web tự chạy các migration còn thiếu khi khởi động; không cần chạy lệnh nâng cấp thủ công. Migration có khóa PostgreSQL chống chạy trùng, dùng `IF NOT EXISTS` và không xóa dữ liệu cũ.

## Tạo tài khoản đầu tiên và chạy

```powershell
python -m app.cli kiem-tra-ket-noi
python -m app.cli tao-tai-khoan
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Mở `http://127.0.0.1:8000/dang-nhap`. Route kiểm tra sức khỏe là `http://127.0.0.1:8000/health`.

## Cấu hình và chạy Tra cứu bằng AI

Hệ thống AI dùng Ollama chạy hoàn toàn trên máy local và không cần khóa API AI. Chạy lệnh sau một lần để tải Ollama bản độc lập và hai mô hình vào ngay trong thư mục dự án:

```powershell
.\cai_dat_ollama.ps1
```

Mặc định dự án dùng `qwen3:4b` cho câu lệnh phức tạp và các đoạn PDF cần xác minh ngữ cảnh. `qwen3-embedding:0.6b` chỉ còn dùng ở luồng AI cũ; Tra cứu bằng AI với điều kiện MDPI hoặc biến trong mô hình không tạo embedding. Các tệp nặng nằm trong `.ollama-runtime` và `.ollama-models`; hai thư mục này đã được loại khỏi Git.

Yêu cầu không khớp quy tắc được Ollama phân loại bằng một JSON ngắn trước. Nếu phân loại đủ điều kiện, phần tra cứu tiếp tục bằng quy tắc và PDF; trường hợp quá phức tạp mới dùng schema phân tích đầy đủ. Timeout mặc định 900 giây dành cho các lần gọi Ollama lâu trên CPU, không làm nhanh thêm quá trình sinh token.

Script cài đặt đọc tên model từ `.env` và không tự tải thêm model khác.

```dotenv
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3:4b
OLLAMA_EMBEDDING_MODEL=qwen3-embedding:0.6b
OLLAMA_TIMEOUT_SECONDS=900
AI_RELEVANCE_THRESHOLD=70
AI_TOP_CHUNKS=8
AI_MAX_RETRIES=3
AI_LOOKUP_WORKERS=2
AI_ALLOW_PDF_VISION=false
PDF_MAX_REDIRECTS=3
PDF_MAX_RETRIES=3
PDF_DOWNLOAD_WORKERS=6
PDF_DOWNLOAD_PER_HOST=2
PDF_BROWSER_ENABLED=true
PDF_BROWSER_PROFILE_DIR=.pdf-browser-profile
  PDF_BROWSER_HEADLESS=true
# Chỉ dùng khi Chrome cá nhân đã mở cổng điều khiển trên máy này:
PDF_BROWSER_CDP_URL=
UNPAYWALL_EMAIL=dia-chi-lien-he@example.com
```

Sau khi khởi động lại máy, chạy Ollama trước khi dùng Tra cứu bằng AI:

```powershell
.\chay_ollama.ps1
```

Không cần mở cửa sổ PowerShell khác hoặc chạy worker riêng. Sau khi metadata của lần tìm kiếm được lưu, tiến trình web tải đồng thời tối đa `PDF_DOWNLOAD_WORKERS` tài liệu (mặc định 6, nhận giá trị từ 1 đến 20), nhưng chỉ cho phép tối đa `PDF_DOWNLOAD_PER_HOST` request tới cùng một hostname (mặc định 2, nhận giá trị từ 1 đến 20). Mỗi luồng có session database và HTTP client riêng; PostgreSQL nhận job bằng `FOR UPDATE SKIP LOCKED` nên hai đợt chạy không lấy cùng một job.

Trang đề tài có panel **Trình tải PDF** với đúng hai tab và bộ chọn lần tìm kiếm. Tab **Tiếp tục tải** chỉ chuẩn bị tài liệu thuộc lần tìm kiếm đã chọn, tiếp tục job đang chờ/tạm dừng và chỉ quét lại trạng thái `khong_tim_thay` khi người dùng chủ động bấm nút; tab này không tự reset lỗi. Tab **Tải lại lỗi** chỉ đưa job `loi` của lần đó về hàng đợi, đặt lại số lần thử và không hiển thị danh sách lỗi chi tiết. Hai tab cùng hiển thị số liệu, tiến độ và thời gian chạy. Nút dừng không hủy request đang chạy: hệ thống ngừng nhận job mới rồi chờ các tệp hiện tại kết thúc.

Trong từng tài liệu, các URL được chuẩn hóa, loại trùng và thử tuần tự theo thứ tự ưu tiên. Lỗi 404, HTML, MIME sai, URL không an toàn, vượt dung lượng hoặc nguồn cần đăng nhập chỉ làm hệ thống chuyển sang URL kế tiếp. Timeout, lỗi DNS/kết nối, HTTP 429 và HTTP 5xx tạm thời được retry có giới hạn với exponential backoff và `Retry-After`; sau đó hệ thống vẫn thử nguồn tiếp theo. Nếu máy chủ yêu cầu chờ quá 30 giây, hệ thống tạm bỏ qua máy chủ đó đến hết thời gian yêu cầu và tiếp tục các nguồn khác, tránh làm đứng cả hàng đợi. Một nhà cung cấp lỗi không làm hỏng toàn bộ tài liệu. Nếu không có PDF hợp lệ sau các nguồn khả dụng, job thành `khong_tim_thay`; chỉ khi mọi kiểm tra mạng đều thất bại hoặc có lỗi nội bộ nghiêm trọng mới thành `loi`. Log ghi tên nguồn, hostname, loại kết quả và lý do, không ghi URL đầy đủ hay thông tin đăng nhập. Tra cứu bằng AI cũng tự bắt đầu sau khi người dùng gửi yêu cầu.

Các hàng đợi vẫn được lưu trong PostgreSQL và dùng `FOR UPDATE SKIP LOCKED` để tránh hai tiến trình nhận cùng một công việc. Tra cứu AI xử lý tối đa `AI_LOOKUP_WORKERS` tài liệu cùng lúc (mặc định 2), mỗi tài liệu có session riêng. Luồng AI không tìm Scopus/WoS và không tải PDF mới. Điều kiện đơn giản bằng tiếng Việt hoặc tiếng Anh được phân tích bằng quy tắc. Nhà xuất bản, năm, DOI và tiêu đề được lọc trước PDF. Với yêu cầu `Phải liên quan đến Malaysia` hoặc `must be related to a bank`, hệ thống nhận chủ đề ngay, xét tiêu đề, tóm tắt và nội dung nghiên cứu; việc chỉ nhắc trong tài liệu tham khảo không đủ điều kiện. Với điều kiện biến trong mô hình, hệ thống tìm cụm từ và từ đồng nghĩa, giữ phần và số trang, rồi chỉ gọi Ollama một lần khi bằng chứng chưa rõ. Chỉ xuất hiện trong tổng quan hoặc tài liệu tham khảo thì không đạt.

Các câu đơn như `Phải có liên quan đến Malaysia`, `Liên quan Malaysia`, `Phải có Malaysia`, `Không liên quan đến Malaysia` và `Không có Malaysia` được hiểu nhanh mà không gọi Ollama để phân tích yêu cầu. “Liên quan” xét chủ đề nghiên cứu; “phải có” và “không có” xét sự xuất hiện trong metadata hoặc PDF. Câu nhiều điều kiện hoặc không khớp các cách diễn đạt đã hỗ trợ được Ollama phân tích bằng tối đa hai lượt JSON ngắn, mỗi lượt giới hạn 120 giây. `Có tác giả là người Việt Nam` là điều kiện bắt buộc; `không phải là bài của Việt Nam` là điều kiện loại trừ. Quốc gia được đối chiếu theo phần tác giả và đơn vị công tác ở đầu PDF; nếu PDF không nêu rõ thông tin này, kết quả là chưa xác định.

Văn bản PDF, đoạn và vị trí cụm từ được cache trong `uploads/.ai-cache` theo SHA-256 và phiên bản bộ đọc/chia đoạn. Kết quả xác minh còn được cache theo model và phiên bản prompt. Lần tra cứu lặp lại cùng PDF và cùng điều kiện sẽ dùng cache; thay PDF hoặc phiên bản pipeline sẽ tự tạo khóa mới. Bộ đếm thời gian của tra cứu AI bắt đầu khi gửi biểu mẫu, tiếp tục trên trang kết quả và lưu thời gian hoàn tất trong lịch sử. Cột thời gian được tự bổ sung khi ứng dụng khởi động nếu cơ sở dữ liệu chưa có.

Yêu cầu, metadata và nội dung PDF chỉ được xử lý bởi Ollama trên máy local, không gửi tới dịch vụ AI bên ngoài và không phát sinh phí API. Cấu hình hiện tại đọc lớp văn bản của PDF; giữ `AI_ALLOW_PDF_VISION=false`. PDF quét ảnh không có lớp văn bản sẽ được gắn nhãn "Chưa đánh giá".

Trình tải PDF thử các nguồn HTTPS công khai bằng HTTP trước, sau đó dùng Chrome chạy ẩn khi trang chặn HTTP hoặc chỉ hiển thị nút PDF trong trình duyệt. Nếu muốn dùng phiên đã đăng nhập trong Chrome thường, cài công cụ kết nối bằng `npm ci --prefix .browser-tools`, mở `chrome://inspect/#remote-debugging` trong Chrome và bật **Remote Debugging**. Trên trang đề tài, bấm **Kết nối Chrome thường để tải PDF**, rồi cho phép yêu cầu trong hộp thoại của Chrome. Sau đó bấm **Tiếp tục tải PDF**. Kết nối này có quyền truy cập các tab và phiên đăng nhập trong Chrome; chỉ cho phép khi bạn tin tưởng ứng dụng. Web mở tab riêng để tìm bài và đóng sau khi xử lý, không xuất cookie ra tệp. Có thể dùng `PDF_BROWSER_CDP_URL=http://127.0.0.1:9222` với hồ sơ Chrome tùy chỉnh. Web không tự giải CAPTCHA và không vượt quyền truy cập của nhà xuất bản.

Nguồn nhanh được ưu tiên trước: URL PDF từ metadata Scopus/WoS, link PDF/full text trong dữ liệu nguồn, Elsevier nếu có quyền API và quy tắc URL công khai của nhà xuất bản đã biết như IRJMS, SAGE. Sau đó hệ thống tra Crossref, Unpaywall khi cấu hình `UNPAYWALL_EMAIL`, OpenAlex, Semantic Scholar, trang DOI và ResearchGate theo tên bài khớp chính xác. Nếu nguồn gửi sai `Content-Type` nhưng nội dung có chữ ký PDF thật, web vẫn lưu tệp. Các tệp trả về HTML, vượt dung lượng hoặc không đúng chữ ký PDF vẫn bị từ chối. `UNPAYWALL_EMAIL` cũng được gửi cho Crossref và OpenAlex để dùng nhóm truy cập lịch sự, giảm nguy cơ bị giới hạn tốc độ. Trên Windows, ứng dụng dùng kho chứng chỉ của hệ điều hành để vẫn kiểm tra TLS đúng cách trong mạng cơ quan.

Nếu toàn bộ job cùng báo lỗi kết nối trên Windows, đặc biệt có `WinError 10013`, hãy dừng đợt tải và kiểm tra tiến trình web có quyền truy cập Internet ra ngoài hay không trước khi bấm **Tải lại lỗi**. Đây là lỗi môi trường chạy, không phải bằng chứng rằng tài liệu không có PDF; không nên lặp lại hàng đợi khi tiến trình vẫn đang bị chặn mạng.

## Cấu hình Scopus và Web of Science

Điền `SCOPUS_API_KEY` và, nếu đơn vị cung cấp, `SCOPUS_INST_TOKEN` để dùng Scopus Search API. Điền `WOS_API_KEY` để dùng Web of Science API Expanded. Endpoint mặc định trong `.env.example` là endpoint chính thức; chỉ thay đổi khi nhà cung cấp hoặc tổ chức cấp endpoint khác.

Khi chọn Q1–Q4 lúc tìm Scopus, ứng dụng lấy CiteScore percentile của năm hoàn chỉnh mới nhất từ Serial Title API và dùng thứ hạng cao nhất trong các ngành của tạp chí. Tài liệu không xác định được quartile không nằm trong kết quả đã chọn Q. Sau khi tìm, trang tự hiển thị tài liệu của lần tìm kiếm vừa thực hiện; có thể đặt lại bộ lọc để xem toàn bộ thư viện đề tài.

Nếu thiếu khóa, ứng dụng vẫn khởi động và mọi chức năng khác vẫn dùng được. Khi tìm kiếm nguồn chưa cấu hình, giao diện hiển thị thông báo rõ ràng và transaction không lưu dở dữ liệu. `SEARCH_MAX_RESULTS` giới hạn số bản ghi tải từ mỗi nguồn trong một lần tìm để kiểm soát hạn mức API.

## Kiểm thử

```powershell
python -m compileall -q app tests
python -m pytest
```

Kiểm thử đơn vị không gọi API thật. Adapter API được kiểm tra bằng HTTP mock. Kiểm thử tích hợp PostgreSQL chỉ chạy khi có `TEST_DATABASE_URL`; nếu thiếu, pytest tự động bỏ qua. Biến này bắt buộc phải trỏ tới database dành riêng cho kiểm thử, không trỏ tới database thật trong `.env`.

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://user:password@localhost:5432/web_research_materials_test"
python -m pytest -m integration
```

Database test cần được tạo trước bằng chính `schema.sql`. Test dùng transaction bao ngoài và rollback sau mỗi ca.

## Chức năng đã hoàn thành

- Đăng nhập, đăng xuất, cookie `HttpOnly`/`SameSite=Lax`, CSRF và mật khẩu Argon2.
- Quản lý tài khoản; không cho tự xóa tài khoản đang đăng nhập; xử lý thân thiện lỗi khóa ngoại.
- CRUD trạng thái đề tài và trạng thái tài liệu với kiểm tra màu.
- CRUD đề tài và lazy-load 10 dòng mỗi lần từ PostgreSQL.
- Chi tiết đề tài, lịch sử tìm kiếm, bộ lọc và sắp xếp tài liệu tại PostgreSQL.
- Adapter độc lập cho Scopus và Web of Science, timeout, phân trang, chuẩn hóa và loại trùng.
- Hai trường từ khóa `OR` và `AND` hỗ trợ nhiều cụm phân cách bằng dấu phẩy, chấm phẩy hoặc xuống dòng. `OR` chỉ yêu cầu một cụm xuất hiện, còn `AND` yêu cầu tất cả cụm xuất hiện.
- Ghi nhận mọi lần phát hiện tài liệu bằng `de_tai_tai_lieu_lan_tim_kiem`.
- Chi tiết tài liệu, metadata nguồn, PDF đính kèm và route xem tệp có xác thực.
- Trạng thái tài liệu riêng cho từng người, lịch sử bất biến, nhiều ảnh minh họa và thống kê từ hai view có sẵn.
- Kiểm tra phần mở rộng, MIME type, chữ ký tệp, SHA-256 và chống path traversal.
- Tra cứu bằng AI local với Ollama, Structured Output theo từng điều kiện, bằng chứng có số trang và lịch sử riêng cho từng đề tài.
- Màn hình Tra cứu bằng AI chọn đề tài trước, hiển thị danh sách tài liệu và lọc PostgreSQL theo lần đánh giá, nhãn `Phù hợp`, `Không phù hợp`, `Chưa đánh giá` cùng các bộ lọc tài liệu chuẩn.
- Tải PDF công khai an toàn với chống SSRF, xác minh lại chuyển hướng, MIME, chữ ký và giới hạn dung lượng.
- Tự tải PDF song song có giới hạn trong tiến trình web, quản lý tiếp tục/lỗi bằng hai tab và hiển thị thời gian theo `HH giờ MM phút SS giây`.

Tìm kiếm Scopus hoặc Web of Science chỉ hoạt động sau khi có khóa API hợp lệ và quyền truy cập phù hợp từ nhà cung cấp.
