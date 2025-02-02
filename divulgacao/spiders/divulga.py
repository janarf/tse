import os
import json
import datetime
import scrapy
import logging
import urllib.parse

from divulgacao.common.fileinfo import FileInfo

class DivulgaSpider(scrapy.Spider):
    name = 'divulga'
    
    # Sim env
    # HOST="https://resultados-sim.tse.jus.br"
    # ENVIRONMENT="teste"
    # CYCLE="ele2022"
    # ELECTIONS=[9240, 9238]

    # Prod env
    HOST="https://resultados.tse.jus.br"
    ENVIRONMENT="oficial"
    CYCLE="ele2022"
    ELECTIONS=[544, 546, 548]

    STATES = "BR AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO ZZ".lower().split()

    BASEURL=f"{HOST}/{ENVIRONMENT}"
            
    def persist_response(self, response, filedate=None):
        url_path = os.path.relpath(urllib.parse.urlparse(response.url).path, "/")
        target_path = os.path.join(self.settings["FILES_STORE"], url_path)
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, "wb") as f:
            f.write(response.body)

        if filedate:
            dt_epoch = filedate.timestamp()
            os.utime(target_path, (dt_epoch, dt_epoch))

    def load_states_index(self, election):
        files_store = self.settings['FILES_STORE']
        base_path = f"{files_store}/{self.ENVIRONMENT}/{self.CYCLE}/{election}/config"
        for state in self.STATES:
            file_path = f"{base_path}/{state}/{state}-e{election:06}-i.json"
            if not os.path.exists(file_path):
                continue

            size = 0
            added = 0

            with open(file_path, "r") as f:
                data = json.loads(f.read())
                for info, filedate in FileInfo.expand_index(state, data):
                    size += 1

                    target_path = f"{files_store}/{self.ENVIRONMENT}/{self.CYCLE}/{info.path}"
                    
                    if not os.path.exists(target_path):
                        logging.debug(f"Target path not found, skipping index {info.filename}")
                        continue
                    
                    modified_time = datetime.datetime.fromtimestamp(os.path.getmtime(target_path))
                    if filedate != modified_time:
                        logging.debug(f"Index date mismatch, skipping index {info.filename} {modified_time} > {filedate}")
                        continue

                    self.index[info.filename] = filedate
                    added += 1
    
            logging.info(f"Loaded index from: {election}-{state}, size {size}, added {added}")

    def json_serialize(self, obj):
        if isinstance(obj, (datetime.datetime, datetime.date)):
            return obj.strftime("%d/%m/%Y %H:%M:%S")

        raise TypeError("Type %s not serializable" % type(obj))            

    def json_parse(self, json_dict):
        for (key, value) in json_dict.items():
            try:
                json_dict[key] = datetime.datetime.strptime(value, "%d/%m/%Y %H:%M:%S")
            except:
                pass
        return json_dict

    def load_index(self):
        files_store = self.settings['FILES_STORE']
        index_path = f"{files_store}/{self.ENVIRONMENT}/index.json"
        try:
            with open(index_path, "r") as f:
                self.index = json.loads(f.read(), object_hook=self.json_parse)

            logging.info(f"Index {index_path} loaded")
        except:
            logging.info("No valid saved index found, loading from downloaded index files")
            self.index = {}
            for election in self.ELECTIONS:
                self.load_states_index(election)

        logging.info(f"Index size {len(self.index)}")

    def save_index(self):
        files_store = self.settings['FILES_STORE']
        index_path = f"{files_store}/{self.ENVIRONMENT}/index.json"
        os.makedirs(os.path.dirname(index_path), exist_ok=True)
        with open(index_path, "w") as f:
            json.dump(self.index, f, default=self.json_serialize)

    def start_requests(self):
        logging.info(f"Host: {self.HOST}")
        logging.info(f"Environment: {self.ENVIRONMENT}")
        logging.info(f"Cycle: {self.CYCLE}")

        self.load_index()
        
        self.pending = set()

        yield from self.query_common()

    def closed(self, reason):
        self.save_index()
        return

    def query_common(self):
        yield scrapy.Request(f"{self.BASEURL}/comum/config/ele-c.json", self.parse_config, dont_filter=True)

    def parse_config(self, response):
        self.persist_response(response)

        for election in self.ELECTIONS:
            logging.info(f"Queueing election: {election}")
            yield from self.generate_requests_index(election)

    def generate_requests_index(self, election):
        config_url = f"{self.BASEURL}/{self.CYCLE}/{election}/config"
            
        for state in self.STATES:
            filename = f"{state}-e{election:06}-i.json"
            logging.debug(f"Queueing index file {filename}")
            yield scrapy.Request(f"{config_url}/{state}/{filename}", self.parse_index, errback=self.errback_index,
                dont_filter=True, priority=2, cb_kwargs={"election": election, "state":state})

    def parse_index(self, response, election, state):
        self.persist_response(response)

        current_index = self.index

        size = 0
        added = 0

        data = json.loads(response.body)
        for info, filedate in FileInfo.expand_index(state, data):
            size += 1

            if info.filename in current_index and filedate <= current_index[info.filename]:
                continue

            if info.filename in self.pending:
                logging.debug(f"Skipping pending duplicated query {info.filename}")
                continue

            self.pending.add(info.filename)
            added += 1

            priority = 0 if info.type == "v" else 2

            logging.debug(f"Queueing file {info.filename} [{current_index.get(info.filename)} > {filedate}]")

            yield scrapy.Request(f"{self.BASEURL}/{self.CYCLE}/{info.path}", self.parse_file, errback=self.errback_file, priority=priority,
                dont_filter=True, cb_kwargs={"info": info, "filedate": filedate})

        logging.info(f"Parsed index for {election}-{state}, size {size}, added {added}, total pending {len(self.pending)}")

    def errback_index(self, failure):
        logging.error(f"Failure downloading {str(failure.request)} - {str(failure.value)}")

    def parse_file(self, response, info, filedate):
        self.persist_response(response, filedate)
        self.index[info.filename] = filedate
        self.pending.discard(info.filename)

        if info.type == "f" and info.ext == "json":
            try:
                data = json.loads(response.body)
                yield from self.query_pictures(data, info)
            except json.JSONDecodeError:
                logging.warning(f"Malformed json at {info.filename}, skipping parse")
                pass

    def errback_file(self, failure):
        logging.error(f"Failure downloading {str(failure.request)} - {str(failure.value)}")
        self.pending.discard(failure.request.cb_kwargs["info"].filename)

    def expand_candidates(self, data):
        for agr in data["carg"]["agr"]:
            for par in agr["par"]:
                for cand in par["cand"]:
                    yield cand

    def query_pictures(self, data, info):
        for cand in self.expand_candidates(data):
            sqcand = cand["sqcand"]
            filename = f"{sqcand}.jpeg"

            if filename in self.pending:
                continue

            # President is br, others go on state specific directories
            cand_state = info.state if info.cand != "1" else "br"

            path = f"{self.CYCLE}/{info.election}/fotos/{cand_state}/{filename}"

            target_path = os.path.join(self.settings["FILES_STORE"], self.ENVIRONMENT, path)
            if not os.path.exists(target_path):
                self.pending.add(filename)

                logging.debug(f"Queueing picture {sqcand}.jpeg")
                yield scrapy.Request(f"{self.BASEURL}/{path}", self.parse_picture, priority=1,
                    dont_filter=True, cb_kwargs={"filename": filename})

    def parse_picture(self, response, filename):
        self.persist_response(response)
        self.pending.discard(filename)