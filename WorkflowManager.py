import json
import os
import shutil
from datetime import datetime, timedelta
import logging
from typing import List, Dict, Any
from inputimeout import inputimeout, TimeoutOccurred
from AgentConfig import AgentConfig
from GoogleSheetDownloader import GoogleSheetDownloader
from ExcelSnapshotComparator import ExcelSnapshotComparator

# Thiết lập logging với encoding UTF-8
logging.basicConfig(
    filename='workflow.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)

class WorkflowManager:
    """Quản lý workflow tải Google Sheet và so sánh snapshot."""

    def __init__(
        self,
        config_file: str,
        workflow_config_file: str = 'workflow_config.json',
        state_file: str = 'state.json',
        predecessor_dir: str = 'predecessor',
        current_dir: str = 'current',
        backup_dir: str = 'backup',
        proxies: dict = None
    ):
        """
        Khởi tạo với file cấu hình, config workflow, file trạng thái và các thư mục.

        Args:
            config_file: Đường dẫn đến file JSON chứa thông tin đại lý.
            workflow_config_file: Đường dẫn đến file JSON chứa config workflow.
            state_file: Đường dẫn đến file trạng thái.
            predecessor_dir: Thư mục lưu snapshot predecessor.
            current_dir: Thư mục lưu snapshot current.
            backup_dir: Thư mục lưu bản sao lưu của predecessor.
            proxies: Dictionary chứa cấu hình proxy (nếu có).
        """
        self.config_file = config_file
        self.workflow_config_file = workflow_config_file
        self.state_file = state_file
        self.predecessor_dir = predecessor_dir
        self.current_dir = current_dir
        self.backup_dir = backup_dir
        self.proxies = proxies
        self.state = self._load_state()
        self.configs = AgentConfig.load_from_json(config_file)
        self.workflow_config = self._load_workflow_config()

        # Tạo các thư mục nếu chưa tồn tại
        for directory in [self.predecessor_dir, self.current_dir, self.backup_dir]:
            if not os.path.exists(directory):
                os.makedirs(directory)
                logging.info(f"Đã tạo thư mục {directory}.")

    def _load_state(self) -> Dict[str, Any]:
        """Đọc file trạng thái."""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logging.error(f"Lỗi khi đọc file trạng thái {self.state_file}: {e}")
            return {}

    def _save_state(self) -> None:
        """Lưu trạng thái vào file."""
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logging.error(f"Lỗi khi lưu file trạng thái {self.state_file}: {e}")

    def _load_workflow_config(self) -> Dict[str, Any]:
        """Đọc file config workflow."""
        default_config = {'allowed_key_pattern': r'^[A-Za-z0-9_.-]+$'}
        try:
            if os.path.exists(self.workflow_config_file):
                with open(self.workflow_config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                if not isinstance(config, dict):
                    raise ValueError("File workflow config phải là dictionary.")
                return config
            logging.warning(f"File {self.workflow_config_file} không tồn tại, dùng config mặc định.")
            return default_config
        except Exception as e:
            logging.error(f"Lỗi khi đọc file {self.workflow_config_file}: {e}, dùng config mặc định.")
            return default_config

    def _get_file_name(self, agent_name: str, config: AgentConfig.Config, directory: str) -> str:
        """Tạo tên file theo định dạng {agent_name}_{project_name}.csv."""
        clean_project_name = config.project_name.replace(' ', '_')
        file_extension = self.workflow_config.get('snapshot_file_extension', '.csv')
        file_name = f"{agent_name}_{clean_project_name}{file_extension}"
        return os.path.join(directory, file_name)

    def _find_predecessor_file(self, agent_name: str, config: AgentConfig.Config) -> str:
        """Tìm file predecessor trong thư mục predecessor dựa trên agent_name và project_name."""
        file_path = self._get_file_name(agent_name, config, self.predecessor_dir)
        if os.path.exists(file_path):
            logging.info(f"Tìm thấy file predecessor: {file_path} cho {agent_name}/{config.project_name}")
            return file_path
        return ''

    def _backup_predecessor_files(self) -> None:
        """Sao lưu tất cả file trong thư mục predecessor vào backup với dấu thời gian."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_subdir = os.path.join(self.backup_dir, f"backup_{timestamp}")
        os.makedirs(backup_subdir, exist_ok=True)

        for file_name in os.listdir(self.predecessor_dir):
            src_path = os.path.join(self.predecessor_dir, file_name)
            if os.path.isfile(src_path):
                dst_path = os.path.join(backup_subdir, file_name)
                shutil.copy2(src_path, dst_path)
                logging.info(f"Đã sao lưu {src_path} vào {dst_path}")

    def _download_snapshot(self, agent_name: str, config: AgentConfig.Config, date: str) -> str:
        """Tải Google Sheet và lưu snapshot vào thư mục current."""
        file_name = self._get_file_name(agent_name, config, self.current_dir)

        print(f"Đại lý {agent_name} - Dự án {config.project_name}")

        # Tạo khóa trạng thái dựa trên agent_name và project_name
        agent_state = self.state.get(agent_name, {}).get(config.project_name, {})
        if agent_state.get(f'download_{date}') == 'completed' and os.path.exists(file_name):
            logging.info(f"Bỏ qua tải snapshot {file_name} cho {agent_name}/{config.project_name}: đã hoàn thành.")
            return file_name

        try:
            downloader = GoogleSheetDownloader(config.spreadsheet_id, config.gid, proxies=self.proxies)
            downloader.download(output_file=file_name)

            agent_state[f'download_{date}'] = 'completed'
            self.state.setdefault(agent_name, {}).setdefault(config.project_name, {}).update(agent_state)
            self._save_state()
            logging.info(f"Tải thành công snapshot {file_name} cho {agent_name}/{config.project_name}.")
            return file_name
        except Exception as e:
            logging.error(f"Lỗi khi tải snapshot {file_name} cho {agent_name}/{config.project_name}: {e}")
            return ''

    def _compare_snapshots(self, agent_name: str, config: AgentConfig.Config, predecessor_file: str, current_file: str) -> None:
        """So sánh hai snapshot."""
        agent_state = self.state.get(agent_name, {}).get(config.project_name, {})
        compare_key = f"compare_{os.path.basename(predecessor_file)}_{os.path.basename(current_file)}"
        if agent_state.get(compare_key) == 'completed':
            logging.info(f"Bỏ qua so sánh {predecessor_file} và {current_file} cho {agent_name}/{config.project_name}: đã hoàn thành.")
            return

        try:
            if not os.path.exists(predecessor_file) or not os.path.exists(current_file):
                raise FileNotFoundError(f"Thiếu file: {predecessor_file} hoặc {current_file}")

            comparator = ExcelSnapshotComparator(
                file_predecessor=predecessor_file,
                file_current=current_file,
                key_col=config.key_column,
                check_cols=config.check_columns,
                allowed_key_pattern=self.workflow_config.get('allowed_key_pattern')
            )
            comparator.compare()

            agent_state[compare_key] = 'completed'
            self.state.setdefault(agent_name, {}).setdefault(config.project_name, {}).update(agent_state)
            self._save_state()
            logging.info(f"So sánh thành công {predecessor_file} và {current_file} cho {agent_name}/{config.project_name}.")
        except Exception as e:
            logging.error(f"Lỗi khi so sánh {predecessor_file} và {current_file} cho {agent_name}/{config.project_name}: {e}")

    def _copy_current_to_predecessor(self) -> None:
        """Sao chép tất cả file từ current sang predecessor (ghi đè nếu tồn tại), xóa current và tạo lại."""
        for file_name in os.listdir(self.current_dir):
            src_path = os.path.join(self.current_dir, file_name)
            if os.path.isfile(src_path):
                dst_path = os.path.join(self.predecessor_dir, file_name)
                shutil.copy2(src_path, dst_path)
                logging.info(f"Đã sao chép {src_path} sang {dst_path} (ghi đè nếu tồn tại)")

        # Xóa thư mục current và tạo lại
        shutil.rmtree(self.current_dir)
        os.makedirs(self.current_dir)
        logging.info(f"Đã xóa và tạo lại thư mục {self.current_dir}")

    def run(self, current_date: str = None, predecessor_date: str = None) -> None:
        """Chạy toàn bộ workflow.

        Args:
            current_date: Ngày để theo dõi trạng thái tải (định dạng YYMMDD), mặc định là hôm nay.
            predecessor_date: Không sử dụng, giữ để tương thích.
        """
        # Sao lưu file predecessor trước khi chạy
        self._backup_predecessor_files()

        # Sao chép file từ current sang predecessor
        self._copy_current_to_predecessor()

        # Đặt ngày mặc định nếu không được cung cấp
        if current_date is None:
            current_date = datetime.now().strftime('%y%m%d')

        for agent_config in self.configs:
            logging.info(f"Bắt đầu xử lý đại lý {agent_config.agent_name}...")

            for config in agent_config.configs:
                try:
                    # Tải snapshot hiện tại vào thư mục current
                    current_file = self._download_snapshot(agent_config.agent_name, config, current_date)
                    if not current_file:
                        logging.warning(f"Bỏ qua so sánh cho {agent_config.agent_name}/{config.project_name}: không có snapshot current.")
                        print("\n=======================")
                        continue

                    # Tìm file predecessor từ thư mục predecessor
                    predecessor_file = self._find_predecessor_file(agent_config.agent_name, config)
                    if not predecessor_file:
                        logging.warning(f"Bỏ qua so sánh cho {agent_config.agent_name}/{config.project_name}: không tìm thấy snapshot predecessor.")
                        print("\n=======================")
                        continue

                    # So sánh snapshot
                    self._compare_snapshots(agent_config.agent_name, config, predecessor_file, current_file)

                except Exception as e:
                    logging.error(f"Lỗi tổng quát khi xử lý {agent_config.agent_name}/{config.project_name}: {e}")
                    print("\n=======================")
                    continue

    def reset_state(self) -> None:
        """Xóa trạng thái cho snapshot current và trạng thái so sánh."""
        current_date = datetime.now().strftime('%y%m%d')
        new_state = {}

        for agent_name, agent_data in self.state.items():
            new_agent_data = {}
            for project_name, agent_state in agent_data.items():
                new_agent_state = {}
                for key, value in agent_state.items():
                    if False:
                        new_agent_state[key] = value
                if new_agent_state:
                    new_agent_data[project_name] = new_agent_state
            if new_agent_data:
                new_state[agent_name] = new_agent_data

        self.state = new_state
        self._save_state()
        logging.info("Đã reset trạng thái cho snapshot current và trạng thái so sánh.")

# Chạy ví dụ
if __name__ == "__main__":
    def get_user_input_with_timeout(prompt, timeout, default):
        try:
            user_input = inputimeout(prompt=prompt, timeout=timeout)
            return user_input.strip().lower()
        except TimeoutOccurred:
            print(f"\nHết thời gian, sử dụng giá trị mặc định: {default}")
            return default

    try:
        proxies = {
            'http': 'http://rb-proxy-apac.bosch.com:8080',
            'https': 'http://rb-proxy-apac.bosch.com:8080'
        }
        manager = WorkflowManager(config_file='project_config.json', proxies=proxies)
        need_reset = get_user_input_with_timeout(
            prompt="Bạn có muốn reset trạng thái không? (y/n): ",
            timeout=5,  # Timeout 5 giây
            default="n"  # Mặc định là 'n'
        )
        if need_reset == 'y':
            manager.reset_state()
        manager.run()
    except Exception as e:
        logging.error(f"Lỗi workflow: {e}")