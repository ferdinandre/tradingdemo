#include "AlpacaPaperApi.hpp"
#include "liveloop.hpp"

int main(int argc, char** argv) {
    AlpacaPaperApi alpaca(argc >= 2 ? argv[1] : "alpaca.toml");
    live_loop(alpaca);
}