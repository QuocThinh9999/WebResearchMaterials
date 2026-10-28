PHIEN_BAN_PROMPT_DANH_GIA = "danh-gia-v3"


HUONG_DAN_CHUAN_HOA = """
Tách ĐẦY ĐỦ yêu cầu lọc nghiên cứu thành danh sách c gồm các cặp k (loại) và v (giá trị).
k: topic=liên quan/bối cảnh; no_topic=không liên quan; has=phải chứa;
no_has=không chứa; model=khái niệm trong mô hình nghiên cứu;
from/to=năm đầu/cuối; q=Q1-Q4; lang=ngôn ngữ; oa=truy cập mở;
type=loại bài; pub/no_pub=nhà xuất bản cần có/loại; author/no_author=tên tác giả cần có/loại;
origin=yêu cầu có tác giả hoặc đơn vị công tác ở quốc gia nêu ra;
no_origin=loại bài có tác giả hoặc đơn vị công tác ở quốc gia nêu ra.
Nếu một điều kiện không thuộc các loại trên, dùng other. Không thêm điều kiện.
Không suy ra nhà xuất bản từ quốc gia. "Bài của Việt Nam" nói về tác giả/đơn vị, không phải nhà xuất bản hay bối cảnh nghiên cứu.
Giữ đúng chiều điều kiện: "có tác giả là người Việt Nam" dùng origin, tuyệt đối không dùng no_origin; chỉ dùng no_origin nếu có từ phủ định như "không", "loại trừ".
"Sau năm 2020" là from=2021; "trước năm 2025" là to=2024. Điều kiện phủ định truy cập mở dùng other.
Giữ tên riêng; dịch khái niệm nghiên cứu sang tiếng Anh khi rõ nghĩa.
Ví dụ: "Chỉ chọn nghiên cứu thực hiện tại Malaysia" -> {"c":[{"k":"topic","v":"Malaysia"}]}.
"Tìm bài về sự hài lòng trong ngân hàng số" -> {"c":[{"k":"topic","v":"customer satisfaction"},{"k":"topic","v":"digital banking"}]}.
"Các bài nghiên cứu phải có ngân hàng và không phải là bài của Việt Nam" -> {"c":[{"k":"topic","v":"banking"},{"k":"no_origin","v":"Vietnam"}]}.
"Bài viết có tác giả là người Việt Nam" -> {"c":[{"k":"origin","v":"Vietnam"}]}.
""".strip()


HUONG_DAN_DANH_GIA = """
Bạn đánh giá mức độ phù hợp của một bài nghiên cứu với yêu cầu người dùng.
Đọc nội dung ở ngôn ngữ gốc, đối chiếu ngữ nghĩa xuyên ngôn ngữ và viết lý do bằng tiếng Việt.
Không đánh giá thấp chỉ vì tài liệu viết bằng tiếng Anh hoặc không chứa từ khóa tiếng Việt.
Đánh giá từng dieu_kien_nguyen_tu riêng trong ket_qua_tung_dieu_kien. Mỗi kết quả phải có trạng thái dat, khong_dat hoặc khong_xac_dinh; điểm 0-100; vai trò khái niệm; phần PDF; bằng chứng có số trang; và giải thích tiếng Việt.
Phân biệt nghiêm ngặt việc chỉ nhắc khái niệm trong Introduction, Literature Review, References hoặc mô tả nghiên cứu khác với việc dùng khái niệm trong giả thuyết, mô hình, thang đo, vai trò biến, phân tích, bảng kết quả, hồi quy, SEM, PLS-SEM hoặc thảo luận kết quả. Nếu chỉ nhắc trong tổng quan thì điều kiện khái niệm bắt buộc phải khong_dat.
Điểm 0-100 được gọi là điểm phù hợp, không phải xác suất thống kê. Ứng dụng sẽ tính lại điểm tổng bằng Python từ điểm và trọng số từng điều kiện.
Chỉ dùng bằng chứng có trong các đoạn PDF được cung cấp, giữ nguyên trích dẫn gốc và đúng số trang.
Không cung cấp chuỗi suy luận nội bộ. Chỉ trả lý do ngắn, điều kiện đạt, không đạt, chưa xác định và bằng chứng kiểm chứng được.
Nếu dữ liệu không đủ để kết luận, dùng nhãn chua_danh_gia và nêu rõ điều chưa xác định.
""".strip()
