### Fixed

- The PostgreSQL restore drill counts each restored table without its inheritance children (`count(*) FROM ONLY`), as the dump's COPY data holds them, so a database with classic table inheritance no longer fails the drill every night after a correct restore (#247)
